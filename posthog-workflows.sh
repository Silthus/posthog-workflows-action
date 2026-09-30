#!/usr/bin/env bash
# shellcheck disable=SC2016 # The jq programs and markdown are single-quoted on purpose.
# Checks or applies PostHog workflow YAML files through the hog_flows code_check and code_apply endpoints.
# Reads its inputs from the environment: POSTHOG_API_KEY, POSTHOG_PROJECT_ID, POSTHOG_HOST, WORKFLOW_FILES, MODE.
set -euo pipefail

readonly JQ_HELPERS='
def data: tostring | gsub("%"; "%25") | gsub("\r"; "%0D") | gsub("\n"; "%0A");
def prop: data | gsub(":"; "%3A") | gsub(","; "%2C");
def cell: tostring | gsub("\\|"; "\\|") | gsub("\n"; " ");
def people: if . == 1 then "1 person" else "\(.) people" end;
def location: "\($file)" + (if .line then ":\(.line)" else "" end) + (if .line and .column then ":\(.column)" else "" end);
def annotation_position: (if .line then ",line=\(.line)" else "" end) + (if .line and .column then ",col=\(.column)" else "" end);
'

readonly PRINT_ERRORS='
.errors[]
| "\(location): \(.status): \(.message)\n  why: \(.why)\n  fix: \(.fix)",
  "::error file=\($file | prop)\(annotation_position),title=\(.status | prop)::\("\(.message)\nWhy: \(.why)\nFix: \(.fix)" | data)"
'

readonly PRINT_RESULT='
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
((.warnings // [])[]
  | "\($file): warning: \(.message)\n  fix: \(.fix)",
    "::warning file=\($file | prop)::\("\(.message)\nFix: \(.fix)" | data)")
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
  + (if $plan.in_flight_runs then " People in it now: \($plan.in_flight_runs)." else "" end)
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
  if [[ -z "${POSTHOG_API_KEY:-}" ]]; then
    echo "::notice title=PostHog workflows::No PostHog API key is set, so no workflow file was checked or applied. Fork pull requests get no secrets, so this is expected there."
    exit 0
  fi
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "::add-mask::${POSTHOG_API_KEY}"
  fi

  local project_id="${POSTHOG_PROJECT_ID:-}"
  [[ "$project_id" =~ ^[0-9]+$ ]] || fail "project-id must be a PostHog project id, a number. Got '${project_id}'."
  local host="${POSTHOG_HOST:-https://us.posthog.com}"
  local mode
  mode="$(resolve_mode "${MODE:-auto}")"
  [[ -n "$mode" ]] || fail "mode must be auto, check or apply. Got '${MODE}'."
  local url="${host%/}/api/projects/${project_id}/hog_flows/code_${mode}/"
  summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
  response="$(mktemp)"
  trap 'rm -f "$response"' EXIT

  local files
  mapfile -t files < <(matching_files "${WORKFLOW_FILES:-workflows/*.yaml}")
  ((${#files[@]} > 0)) || fail "No file matches '${WORKFLOW_FILES:-workflows/*.yaml}'."

  printf '## PostHog workflows: %s\n\n' "$mode" >>"$summary"
  local failures=0 file
  for file in "${files[@]}"; do
    send_file "$file" "$mode" "$url" || failures=$((failures + 1))
  done
  if ((failures > 0)); then
    echo "${failures} of ${#files[@]} workflow file(s) failed to ${mode}."
    exit 1
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
  read -ra patterns <<<"$1"
  for pattern in "${patterns[@]}"; do
    # The pattern is unquoted on purpose so the shell expands the glob.
    # shellcheck disable=SC2206
    local matches=($pattern)
    ((${#matches[@]} == 0)) || printf '%s\n' "${matches[@]}"
  done
}

send_file() {
  local file="$1" mode="$2" url="$3" http_status curl_exit=0
  http_status="$(
    jq -Rs '{content: .}' "$file" |
      curl --silent --show-error --fail-with-body --max-time 60 --connect-timeout 10 \
        --header @<(printf 'Authorization: Bearer %s\n' "$POSTHOG_API_KEY") \
        --header 'Content-Type: application/json' --header 'Accept: application/json' \
        --data-binary @- --output "$response" --write-out '%{http_code}' "$url"
  )" || curl_exit=$?

  if [[ "$http_status" == "000" ]]; then
    report_failure "$file" "Could not reach ${url} (curl exit code ${curl_exit})."
    return 1
  fi
  if ! jq -e 'type == "object"' "$response" >/dev/null 2>&1; then
    report_failure "$file" "PostHog answered HTTP ${http_status} without JSON: $(head -c 300 "$response" | tr -s '\r\n' ' ')"
    return 1
  fi
  if ((curl_exit == 0)); then
    jq -r --arg file "$file" --arg mode "$mode" "${JQ_HELPERS}${PRINT_RESULT}" "$response" || return 1
    jq -r --arg file "$file" "${JQ_HELPERS}${SUMMARY_RESULT}" "$response" >>"$summary" || return 1
    return 0
  fi
  if jq -e '.errors | type == "array"' "$response" >/dev/null; then
    jq -r --arg file "$file" "${JQ_HELPERS}${PRINT_ERRORS}" "$response"
    jq -r --arg file "$file" "${JQ_HELPERS}${SUMMARY_ERRORS}" "$response" >>"$summary"
  else
    report_failure "$file" "PostHog answered HTTP ${http_status}: $(jq -r '.detail // (tostring | .[:300])' "$response")"
  fi
  return 1
}

report_failure() {
  local file="$1" message="$2"
  echo "${file}: ${message}"
  jq -rn --arg file "$file" --arg message "$message" \
    "${JQ_HELPERS}"'"::error file=\($file | prop)::\($message | data)"'
  printf '### `%s`: failed\n\n%s\n\n' "$file" "$message" >>"$summary"
}

fail() {
  echo "::error title=PostHog workflows::$1"
  exit 1
}

main "$@"
