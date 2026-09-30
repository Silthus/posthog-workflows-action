#!/usr/bin/env bash
# shellcheck disable=SC2016 # The jq programs and markdown are single-quoted on purpose.
# Checks or applies PostHog workflow YAML files through the hog_flows code_check and code_apply endpoints.
# Reads its inputs from the environment: POSTHOG_API_KEY, POSTHOG_PROJECT_ID, POSTHOG_HOST, WORKFLOW_FILES, MODE.
set -euo pipefail

readonly JQ_HELPERS='
def replace($from; $to): split($from) | join($to);
def data: tostring | replace("%"; "%25") | replace("\r"; "%0D") | replace("\n"; "%0A");
def prop: data | replace(":"; "%3A") | replace(","; "%2C");
def cell: tostring | replace("|"; "\\|") | replace("\r"; " ") | replace("\n"; " ");
def people: if . == 1 then "1 person" else "\(.) people" end;
def location: "\($file)" + (if .line then ":\(.line)" else "" end) + (if .line and .column then ":\(.column)" else "" end);
def whole_number: if type == "number" and . == floor then floor else null end;
def annotation_position:
  (.line | whole_number) as $line | (.column | whole_number) as $column
  | if $line == null then ""
    else ",line=\($line)" + (if $column == null then "" else ",col=\($column)" end) end;
'

readonly ERROR_LINES='
.errors[] | "\(location): \(.status): \(.message)\n  why: \(.why)\n  fix: \(.fix)"
'

readonly ERROR_ANNOTATIONS='
.errors[] | "::error file=\($file | prop)\(annotation_position),title=\(.status | prop)::\("\(.message)\nWhy: \(.why)\nFix: \(.fix)" | data)"
'

readonly RESULT_LINES='
def counts: [
  (.added_steps // [] | length | select(. > 0) | "\(.) added"),
  (.changed_steps // [] | length | select(. > 0) | "\(.) changed"),
  (.removed_steps // [] | length | select(. > 0) | "\(.) removed")
] | join(", ");
(if $mode == "apply" then
  "\($file): \(.result) \(.workflow.key // "") (version \(.workflow.version // "?"), \(.workflow.status // "?"))"
else
  .plan as $plan | ($plan | counts) as $counts
  | "\($file): \($plan.result) \($plan.workflow.key // "a new workflow")\(if $counts == "" then "" else " (\($counts))" end)"
end),
((.warnings // [])[] | "\($file): warning: \(.message)\n  fix: \(.fix)")
'

readonly WARNING_ANNOTATIONS='
(.warnings // [])[] | "::warning file=\($file | prop)::\("\(.message)\nFix: \(.fix)" | data)"
'

readonly SUMMARY_RESULT='
def removed_detail:
  if .runs == null then "count unknown"
  elif .moves_to then "\(.runs | people) move to \(.moves_to.name | cell)"
  elif .exits then "\(.runs | people) exit"
  else .runs | people end;
(.result // .plan.result) as $result | .plan as $plan
| "### `\($file)`: \($result)",
  "",
  (if $plan.workflow then "**\($plan.workflow.name | cell)** (`\($plan.workflow.key)`), status \($plan.status.from // "none") → \($plan.status.to)."
   else "A new workflow, status \($plan.status.to)." end)
  + (if ($plan.changed_fields // []) == [] then "" else " Changed fields: \($plan.changed_fields | join(", "))." end)
  + (if $plan.workflow and $plan.in_flight_runs != null then " People in it now: \($plan.in_flight_runs)." else "" end)
  + (if $plan.discards_draft then " Applying it discards a staged draft." else "" end),
  "",
  ( [ ($plan.added_steps // [])[] | "| Added | \(.name | cell) | \(.type) | |" ]
    + [ ($plan.changed_steps // [])[] | "| Changed | \(.name | cell) | \(.type) | \(.changes | join(", ")) |" ]
    + [ ($plan.removed_steps // [])[] | "| Removed | \(.name | cell) | | \(removed_detail) |" ]
    | select(length > 0)
    | "| Change | Step | Type | Detail |", "| --- | --- | --- | --- |", .[], "" )
'

readonly SUMMARY_ERRORS='
"### `\($file)`: \(.errors | length) error(s)",
"",
(.errors[] | "- `\(location)` \(.status): \(.message | cell) \(.fix | cell)"),
""
'

main() {
  local mode
  mode="$(resolve_mode "${MODE:-auto}")"
  [[ -n "$mode" ]] || fail "mode must be auto, check or apply. Got '${MODE}'."
  if [[ -z "${POSTHOG_API_KEY:-}" ]]; then
    report_missing_key "$mode"
    exit 0
  fi
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "::add-mask::$(escape_data "$POSTHOG_API_KEY")"
  fi
  [[ "$POSTHOG_API_KEY" =~ ^[[:graph:]]+$ ]] || fail "api-key must be one API key, without spaces or line breaks."

  local project_id="${POSTHOG_PROJECT_ID:-}"
  [[ "$project_id" =~ ^[0-9]+$ ]] || fail "project-id must be a PostHog project id, a number. Got '${project_id}'."
  local host="${POSTHOG_HOST:-https://us.posthog.com}"
  [[ "$host" == https://* || "$host" =~ ^http://(localhost|127\.0\.0\.1)(:[0-9]+)?/?$ ]] ||
    fail "host must start with https://, so the API key never travels unencrypted. Got '${host}'."
  local url="${host%/}/api/projects/${project_id}/hog_flows/code_${mode}/"
  summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
  response="$(mktemp)"
  curl_errors="$(mktemp)"
  output="$(mktemp)"
  annotations="$(mktemp)"
  trap 'rm -f "$response" "$curl_errors" "$output" "$annotations"' EXIT

  local pattern="${WORKFLOW_FILES:-workflows/*.yaml}" files
  mapfile -d '' -t files < <(matching_files "$pattern")
  if ((${#files[@]} == 0)); then
    printf 'No workflow files match %s; nothing to %s.\n' "$pattern" "$mode" | print_untrusted
    exit 0
  fi

  printf '## PostHog workflows: %s\n\n' "$mode" >>"$summary"
  local failures=0 file
  for file in "${files[@]}"; do
    send_file "$file" "$mode" "$url" >"$output" 2>&1 || failures=$((failures + 1))
    print_untrusted <"$output"
    awk 1 "$annotations"
    : >"$annotations"
  done
  if ((failures > 0)); then
    echo "${failures} of ${#files[@]} workflow file(s) failed to ${mode}."
    exit 1
  fi
}

report_missing_key() {
  if [[ "$1" == "apply" ]]; then
    echo "::warning title=PostHog workflows::No PostHog API key is set, so no workflow file was applied. Check that this job can read the secret that holds the write key."
  else
    echo "::notice title=PostHog workflows::No PostHog API key is set, so no workflow file was checked. Fork pull requests get no secrets, so this is expected there."
  fi
}

resolve_mode() {
  case "$1" in
    check | apply) echo "$1" ;;
    auto)
      if [[ "${GITHUB_EVENT_NAME:-}" == "push" && -n "${DEFAULT_BRANCH:-}" && "${GITHUB_REF:-}" == "refs/heads/${DEFAULT_BRANCH}" ]]; then
        echo apply
      else
        echo check
      fi
      ;;
    *) echo "" ;;
  esac
}

matching_files() {
  shopt -s nullglob globstar
  local patterns pattern
  read -r -d '' -a patterns <<<"$1" || true
  for pattern in "${patterns[@]}"; do
    # The pattern is unquoted on purpose so the shell expands the glob.
    # shellcheck disable=SC2206
    local matches=($pattern) match
    for match in "${matches[@]}"; do
      [[ ! -e "$match" && ! -L "$match" ]] || printf '%s\0' "$match"
    done
  done
}

send_file() {
  local file="$1" mode="$2" url="$3" http_status curl_exit attempt
  if [[ -L "$file" || ! -f "$file" ]]; then
    report_failure "$file" "Not a regular file, so it was not sent."
    return 1
  fi
  for attempt in 1 2; do
    curl_exit=0
    http_status="$(post_file "$file" "$url")" || curl_exit=$?
    if ((attempt == 2)) || ! is_transient "$http_status"; then break; fi
    sleep 2
  done

  if [[ "$http_status" == "000" ]]; then
    report_failure "$file" "Could not reach ${url}: $(head -n 1 "$curl_errors")"
    return 1
  fi
  if ! jq -e 'type == "object"' "$response" >/dev/null 2>&1; then
    report_failure "$file" "PostHog answered HTTP ${http_status} without JSON: $(head -c 300 "$response" | tr -s '\r\n' ' ')"
    return 1
  fi
  if ((curl_exit == 0)); then
    if ! jq -e --arg mode "$mode" 'if $mode == "apply" then .result else .plan.result end | type == "string"' "$response" >/dev/null; then
      report_failure "$file" "PostHog answered HTTP ${http_status} without a result: $(jq -r 'tojson | .[:300]' "$response")"
      return 1
    fi
    jq -r --arg file "$file" --arg mode "$mode" "${JQ_HELPERS}${RESULT_LINES}" "$response" || return 1
    jq -r --arg file "$file" "${JQ_HELPERS}${WARNING_ANNOTATIONS}" "$response" >>"$annotations" || return 1
    jq -r --arg file "$file" "${JQ_HELPERS}${SUMMARY_RESULT}" "$response" >>"$summary" || return 1
    return 0
  fi
  if jq -e '.errors | type == "array"' "$response" >/dev/null; then
    jq -r --arg file "$file" "${JQ_HELPERS}${ERROR_LINES}" "$response"
    jq -r --arg file "$file" "${JQ_HELPERS}${ERROR_ANNOTATIONS}" "$response" >>"$annotations"
    jq -r --arg file "$file" "${JQ_HELPERS}${SUMMARY_ERRORS}" "$response" >>"$summary"
  else
    report_failure "$file" "PostHog answered HTTP ${http_status}: $(jq -r '.detail // . | tostring | .[:1000]' "$response")"
  fi
  return 1
}

post_file() {
  jq -Rs '{content: .}' <"$1" |
    curl --silent --show-error --fail-with-body --max-time 60 --connect-timeout 10 \
      --header @<(printf 'Authorization: Bearer %s\n' "$POSTHOG_API_KEY") \
      --header 'Content-Type: application/json' --header 'Accept: application/json' \
      --data-binary @- --output "$response" --write-out '%{http_code}' "$2" 2>"$curl_errors"
}

is_transient() {
  [[ "$1" =~ ^(000|408|409|429|5[0-9][0-9])$ ]]
}

report_failure() {
  local file="$1" message="$2"
  echo "${file}: ${message}"
  jq -rn --arg file "$file" --arg message "$message" \
    "${JQ_HELPERS}"'"::error file=\($file | prop)::\($message | data)"' >>"$annotations"
  printf '### `%s`: failed\n\n%s\n\n' "$file" "$message" >>"$summary"
}

# GitHub runs any output line that parses as a workflow command, and a "##[" command may start anywhere
# in a line. Text from the API or from a file name is printed with command processing stopped.
print_untrusted() {
  local token
  token="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  echo "::stop-commands::${token}"
  awk 1
  echo "::${token}::"
}

escape_data() {
  local value="${1//'%'/%25}"
  value="${value//$'\r'/%0D}"
  printf '%s' "${value//$'\n'/%0A}"
}

fail() {
  echo "::error title=PostHog workflows::$(escape_data "$1")"
  exit 1
}

main "$@"
