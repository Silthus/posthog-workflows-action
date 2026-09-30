import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "posthog-workflows.sh"
CHECK_PATH = "/api/projects/42/hog_flows/code_check/"
APPLY_PATH = "/api/projects/42/hog_flows/code_apply/"

CLEAN_FILE = "version: 1\nkey: trial-upgrade-nudge\nname: Trial upgrade nudge\n"
BROKEN_FILE = "version: 1\nkey: broken\nsteps:\n  - type: delay\n    duration: 3 days\n"
NEW_FILE = "version: 1\nkey: welcome\nname: Welcome\n"
SAME_FILE = "version: 1\nkey: same\nname: Same\n"

WORKFLOW = {"id": "0199f0c2-0000-0000-0000-000000000001", "key": "trial-upgrade-nudge", "name": "Trial upgrade nudge", "version": 4, "status": "active"}


def plan(result: str, workflow: dict | None = WORKFLOW, **overrides: object) -> dict:
    return {
        "result": result,
        "workflow": workflow,
        "changed_fields": [],
        "status": {"from": workflow and workflow["status"], "to": "active"},
        "added_steps": [],
        "changed_steps": [],
        "removed_steps": [],
        "in_flight_runs": 0,
        "position_unknown": 0,
        "empty_variables": [],
        "schedule_conflicts": [],
        "discards_draft": False,
        **overrides,
    }


UPDATE_PLAN = plan(
    "update",
    changed_fields=["name"],
    added_steps=[{"id": "wait_a_week", "name": "Wait a week", "type": "delay"}],
    changed_steps=[{"id": "which_plan", "name": "Which plan?", "type": "conditional_branch", "changes": ["config.conditions"]}],
    removed_steps=[
        {
            "action_id": "wait_three_days",
            "name": "Wait three days",
            "runs": 41,
            "moves_to": {"action_id": "which_plan", "name": "Which plan?"},
            "exits": False,
        }
    ],
    in_flight_runs=57,
)
REMOVED_STEP_WARNING = {
    "message": "41 people are in Wait three days, which this file removes. They move to Which plan?.",
    "fix": "If you renamed the step, add id: wait_three_days to it to keep them where they are.",
    "path": None,
}
BROKEN_ERRORS = {
    "errors": [
        {
            "status": "invalid_value",
            "message": "steps[0].duration: '3 days' is not a duration.",
            "why": "A delay needs a number followed by one unit: s, m, h or d.",
            "fix": "Write the duration as 3d.",
            "path": "steps[0].duration",
            "line": 5,
            "column": 15,
        },
        {
            "status": "missing_field",
            "message": "The file has no name.",
            "why": "Every workflow needs a name.",
            "fix": "Add name: to the top of the file.",
            "path": None,
            "line": None,
            "column": None,
        },
    ]
}

INJECTION = "\n::set-output name=x::y\n::add-mask::\r::warning::from the api\n##[set-output name=v1;]z ::error::inline"
INJECTED_FILE_NAME = f"evil{INJECTION}.yaml"
LONG_DETAIL = f"Too long{INJECTION}" + "x" * 200_000
MANY_PERCENT_SIGNS = "%" * 200_000

RUNNER_LINE_BREAK = re.compile(r"\r\n|\r|\n")
RUNNER_COMMANDS = {
    "stop-commands", "internal-set-repo-path", "set-env", "set-output", "save-state", "add-mask", "add-path",
    "add-matcher", "remove-matcher", "debug", "warning", "error", "notice", "group", "endgroup", "echo",
}
DATA_ESCAPES = [("\r", "%0D"), ("\n", "%0A"), ("%", "%25")]
PROPERTY_ESCAPES = [("\r", "%0D"), ("\n", "%0A"), (":", "%3A"), (",", "%2C"), ("%", "%25")]


@dataclass(frozen=True)
class WorkflowCommand:
    name: str
    properties: dict[str, str]
    data: str


def unescape(value: str, escapes: list[tuple[str, str]]) -> str:
    for token, replacement in escapes:
        value = value.replace(replacement, token)
    return value


def parse_properties(text: str, separator: str) -> dict[str, str]:
    pairs = [pair.split("=", 1) for pair in text.split(separator) if "=" in pair]
    return {key: unescape(value, PROPERTY_ESCAPES) for key, value in pairs}


def parse_command(line: str, registered: set[str]) -> WorkflowCommand | None:
    stripped = line.lstrip()
    end = stripped.find("::", 2)
    if stripped.startswith("::") and end >= 0:
        name, _, properties = stripped[2:end].partition(" ")
        if name.lower() in registered:
            return WorkflowCommand(name, parse_properties(properties.strip(), ","), unescape(stripped[end + 2 :], DATA_ESCAPES))
    start = line.find("##[")
    end = line.find("]", start)
    if start >= 0 and end >= 0:
        name, _, properties = line[start + 3 : end].partition(" ")
        if name.lower() in registered:
            return WorkflowCommand(name, parse_properties(properties, ";"), line[end + 1 :])
    return None


class RunnerLog:
    """Reads a step's output the way the GitHub runner does: both command syntaxes, and stop-commands."""

    def __init__(self, output: str) -> None:
        self.commands: list[WorkflowCommand] = []
        self.visible: list[str] = []
        stop_token: str | None = None
        for line in RUNNER_LINE_BREAK.split(output.removesuffix("\n")) if output else []:
            registered = RUNNER_COMMANDS if stop_token is None else RUNNER_COMMANDS | {stop_token.lower()}
            command = parse_command(line, registered)
            if stop_token is not None:
                if command and command.name.lower() == stop_token.lower():
                    stop_token = None
                else:
                    self.visible.append(line)
            elif command is None:
                self.visible.append(line)
            elif command.name.lower() == "stop-commands":
                stop_token = command.data
            else:
                self.commands.append(command)
        self.stopped = stop_token is not None


class MockPostHog(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.routes: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
        self.requests: list[dict] = []

    def reply(self, path: str, content: str, status: int, body: object) -> None:
        self.routes[(path, content)] = [(status, "application/json", json.dumps(body))]

    def reply_raw(self, path: str, content: str, status: int, body: str) -> None:
        self.routes[(path, content)] = [(status, "text/html", body)]

    def reply_in_turn(self, path: str, content: str, replies: list[tuple[int, object]]) -> None:
        self.routes[(path, content)] = [(status, "application/json", json.dumps(body)) for status, body in replies]

    def next_reply(self, path: str, content: str) -> tuple[int, str, str]:
        replies = self.routes.get((path, content), [(500, "text/plain", "no route")])
        return replies.pop(0) if len(replies) > 1 else replies[0]


class MockHandler(BaseHTTPRequestHandler):
    server: MockPostHog

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "authorization": self.headers["Authorization"], "content_type": self.headers["Content-Type"], "body": body}
        )
        status, content_type, payload = self.server.next_reply(self.path, body.get("content"))
        encoded = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        pass


class PostHogWorkflowsScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockPostHog()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.workspace = Path(tempfile.mkdtemp())
        (self.workspace / "workflows").mkdir()
        self.summary = self.workspace / "summary.md"

    def write(self, name: str, content: str) -> None:
        (self.workspace / "workflows" / name).write_text(content)

    def run_script(self, **env: str) -> subprocess.CompletedProcess:
        base_env = {
            "PATH": os.environ["PATH"],
            "POSTHOG_API_KEY": "phs_example",
            "POSTHOG_PROJECT_ID": "42",
            "POSTHOG_HOST": f"http://127.0.0.1:{self.server.server_port}/",
            "WORKFLOW_FILES": "workflows/*.yaml",
            "MODE": "check",
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }
        return subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.workspace, env={**base_env, **env}, capture_output=True, text=True, timeout=30
        )

    def test_check_with_a_clean_plan_sends_the_file_and_summarizes_the_plan(self) -> None:
        self.write("trial.yaml", CLEAN_FILE)
        self.server.reply(CHECK_PATH, CLEAN_FILE, 200, {"plan": UPDATE_PLAN, "warnings": [REMOVED_STEP_WARNING]})

        run = self.run_script()

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(
            self.server.requests,
            [{"path": CHECK_PATH, "authorization": "Bearer phs_example", "content_type": "application/json", "body": {"content": CLEAN_FILE}}],
        )
        self.assertIn("workflows/trial.yaml: update trial-upgrade-nudge (1 added, 1 changed, 1 removed)", run.stdout)
        self.assertIn(f"::warning file=workflows/trial.yaml::{REMOVED_STEP_WARNING['message']}%0AFix: {REMOVED_STEP_WARNING['fix']}", run.stdout)
        summary = self.summary.read_text()
        self.assertIn("workflows/trial.yaml", summary)
        self.assertIn("| Added | Wait a week | delay |", summary)
        self.assertIn("| Changed | Which plan? | conditional_branch | config.conditions |", summary)
        self.assertIn("| Removed | Wait three days | | 41 people move to Which plan? |", summary)

    def test_check_with_located_errors_annotates_each_error_and_fails(self) -> None:
        self.write("broken.yaml", BROKEN_FILE)
        self.write("trial.yaml", CLEAN_FILE)
        self.server.reply(CHECK_PATH, BROKEN_FILE, 400, BROKEN_ERRORS)
        self.server.reply(CHECK_PATH, CLEAN_FILE, 200, {"plan": plan("unchanged"), "warnings": []})

        run = self.run_script(WORKFLOW_FILES="workflows/broken.yaml\nworkflows/trial.yaml workflows/deleted.yaml")

        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn(
            "workflows/broken.yaml:5:15: invalid_value: steps[0].duration: '3 days' is not a duration.\n"
            "  why: A delay needs a number followed by one unit: s, m, h or d.\n"
            "  fix: Write the duration as 3d.\n",
            run.stdout,
        )
        self.assertIn(
            "::error file=workflows/broken.yaml,line=5,col=15,title=invalid_value::"
            "steps[0].duration: '3 days' is not a duration.%0AWhy: A delay needs a number followed by one unit: s, m, h or d.%0AFix: Write the duration as 3d.",
            run.stdout,
        )
        self.assertIn("workflows/broken.yaml: missing_field: The file has no name.", run.stdout)
        self.assertIn("::error file=workflows/broken.yaml,title=missing_field::The file has no name.", run.stdout)
        self.assertIn("workflows/trial.yaml: unchanged trial-upgrade-nudge", run.stdout)
        self.assertEqual(run.stderr, "")

    def test_apply_reports_created_updated_and_unchanged(self) -> None:
        self.write("new.yaml", NEW_FILE)
        self.write("same.yaml", SAME_FILE)
        self.write("trial.yaml", CLEAN_FILE)
        created = {**WORKFLOW, "key": "welcome", "name": "Welcome", "version": 1, "status": "draft"}
        same = {**WORKFLOW, "key": "same", "name": "Same"}
        self.server.reply(APPLY_PATH, NEW_FILE, 201, {"result": "created", "workflow": created, "plan": plan("create", workflow=None), "warnings": []})
        self.server.reply(APPLY_PATH, CLEAN_FILE, 200, {"result": "updated", "workflow": {**WORKFLOW, "version": 5}, "plan": UPDATE_PLAN, "warnings": []})
        self.server.reply(APPLY_PATH, SAME_FILE, 200, {"result": "unchanged", "workflow": same, "plan": plan("unchanged", workflow=same), "warnings": []})

        run = self.run_script(MODE="apply")

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual([request["path"] for request in self.server.requests], [APPLY_PATH] * 3)
        self.assertEqual([request["body"] for request in self.server.requests], [{"content": NEW_FILE}, {"content": SAME_FILE}, {"content": CLEAN_FILE}])
        self.assertIn("workflows/new.yaml: created welcome (version 1, draft)", run.stdout)
        self.assertIn("workflows/same.yaml: unchanged same (version 4, active)", run.stdout)
        self.assertIn("workflows/trial.yaml: updated trial-upgrade-nudge (version 5, active)", run.stdout)

    def test_auto_mode_checks_pull_requests_and_applies_pushes_to_the_default_branch(self) -> None:
        self.write("trial.yaml", CLEAN_FILE)
        self.server.reply(CHECK_PATH, CLEAN_FILE, 200, {"plan": plan("unchanged"), "warnings": []})
        self.server.reply(APPLY_PATH, CLEAN_FILE, 200, {"result": "unchanged", "workflow": WORKFLOW, "plan": plan("unchanged"), "warnings": []})
        auto = {"MODE": "auto", "DEFAULT_BRANCH": "main"}

        for event, ref, expected_path in [
            ("pull_request", "refs/pull/7/merge", CHECK_PATH),
            ("push", "refs/heads/feature", CHECK_PATH),
            ("push", "refs/heads/main", APPLY_PATH),
        ]:
            with self.subTest(event=event, ref=ref):
                self.server.requests.clear()
                run = self.run_script(**auto, GITHUB_EVENT_NAME=event, GITHUB_REF=ref)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual([request["path"] for request in self.server.requests], [expected_path])

    def test_a_missing_key_prints_one_line_and_succeeds(self) -> None:
        self.write("trial.yaml", CLEAN_FILE)
        push_to_main = {"MODE": "auto", "DEFAULT_BRANCH": "main", "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}

        for name, env, expected_command in [
            ("check", {"MODE": "check"}, "notice"),
            ("apply", {"MODE": "apply"}, "warning"),
            ("auto on a push to the default branch", push_to_main, "warning"),
        ]:
            with self.subTest(name):
                run = self.run_script(POSTHOG_API_KEY="", **env)

                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual(len(run.stdout.splitlines()), 1, run.stdout)
                self.assertEqual([command.name for command in RunnerLog(run.stdout).commands], [expected_command], run.stdout)
                self.assertIn("No PostHog API key", run.stdout)
                self.assertEqual(run.stderr, "")
                self.assertEqual(self.server.requests, [])

    def test_an_answer_the_script_cannot_read_prints_a_readable_message_and_fails(self) -> None:
        self.write("trial.yaml", CLEAN_FILE)
        for name, mode, status, body, expected in [
            ("server error without JSON", "check", 502, "<html><body>Bad gateway</body></html>", "PostHog answered HTTP 502 without JSON: <html><body>Bad gateway</body></html>"),
            ("check without a plan", "check", 200, "{}", "PostHog answered HTTP 200 without a result: {}"),
            ("apply without a result", "apply", 200, '{"workflow":null}', 'PostHog answered HTTP 200 without a result: {"workflow":null}'),
        ]:
            with self.subTest(name):
                self.server.reply_raw(CHECK_PATH if mode == "check" else APPLY_PATH, CLEAN_FILE, status, body)

                run = self.run_script(MODE=mode)

                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertIn(f"workflows/trial.yaml: {expected}", RunnerLog(run.stdout).visible)
                self.assertEqual(RunnerLog(run.stdout).commands, [WorkflowCommand("error", {"file": "workflows/trial.yaml"}, expected)])

    def test_a_transient_failure_is_retried_once(self) -> None:
        self.write("trial.yaml", CLEAN_FILE)
        applied = {"result": "unchanged", "workflow": WORKFLOW, "plan": plan("unchanged"), "warnings": []}
        conflict = {"errors": [{"status": "conflict", "message": "Another request created this workflow.", "why": "w", "fix": "Apply the file again.", "path": None, "line": None, "column": None}]}
        busy = {"detail": "Request was throttled."}
        for name, replies, exit_code in [
            ("server error, then success", [(503, {"detail": "Unavailable"}), (200, applied)], 0),
            ("rate limit, then success", [(429, busy), (200, applied)], 0),
            ("conflict, then success", [(409, conflict), (200, applied)], 0),
            ("server error twice", [(502, {"detail": "Bad gateway"}), (502, {"detail": "Bad gateway"}), (200, applied)], 1),
        ]:
            with self.subTest(name):
                self.server.requests.clear()
                self.server.reply_in_turn(APPLY_PATH, CLEAN_FILE, replies)

                run = self.run_script(MODE="apply")

                self.assertEqual(run.returncode, exit_code, run.stdout + run.stderr)
                self.assertEqual(len(self.server.requests), 2)

    def test_a_host_without_https_is_refused(self) -> None:
        self.write("trial.yaml", CLEAN_FILE)
        for host in ["http://eu.posthog.example.com", "eu.posthog.example.com", "http://127.0.0.1.example.com", "HTTP://eu.posthog.example.com"]:
            with self.subTest(host):
                run = self.run_script(POSTHOG_HOST=host)

                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertEqual(
                    RunnerLog(run.stdout).commands,
                    [WorkflowCommand("error", {"title": "PostHog workflows"}, f"host must start with https://, so the API key never travels unencrypted. Got '{host}'.")],
                )

    def test_no_matching_file_prints_one_line_and_succeeds(self) -> None:
        run = self.run_script()

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        log = RunnerLog(run.stdout)
        self.assertEqual(log.visible, ["No workflow files match workflows/*.yaml; nothing to check."])
        self.assertEqual(log.commands, [])
        self.assertEqual(self.server.requests, [])

    def test_a_symbolic_link_is_not_sent(self) -> None:
        (self.workspace / "outside.txt").write_text("token: invented-secret\n")
        (self.workspace / "workflows" / "link.yaml").symlink_to(self.workspace / "outside.txt")

        run = self.run_script()

        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(
            RunnerLog(run.stdout).commands, [WorkflowCommand("error", {"file": "workflows/link.yaml"}, "Not a regular file, so it was not sent.")]
        )

    def test_inputs_never_run_a_workflow_command(self) -> None:
        for name, env, expected_commands in [
            ("files", {"WORKFLOW_FILES": f"none/*.yaml{INJECTION}"}, []),
            ("project id", {"POSTHOG_PROJECT_ID": f"1{INJECTION}"}, [WorkflowCommand("error", {"title": "PostHog workflows"}, f"project-id must be a PostHog project id, a number. Got '1{INJECTION}'.")]),
            (
                "api key",
                {"POSTHOG_API_KEY": f"phs_x{INJECTION}", "GITHUB_ACTIONS": "true"},
                [
                    WorkflowCommand("add-mask", {}, f"phs_x{INJECTION}"),
                    WorkflowCommand("error", {"title": "PostHog workflows"}, "api-key must be one API key, without spaces or line breaks."),
                ],
            ),
        ]:
            with self.subTest(name):
                run = self.run_script(**env)

                log = RunnerLog(run.stdout)
                self.assertEqual(log.commands, expected_commands, run.stdout)
                self.assertFalse(log.stopped, run.stdout)
                self.assertEqual(self.server.requests, [])

    def test_text_from_the_api_or_the_file_never_runs_a_workflow_command(self) -> None:
        self.write(INJECTED_FILE_NAME, CLEAN_FILE)
        file = f"workflows/{INJECTED_FILE_NAME}"
        evil_workflow = {**WORKFLOW, "key": INJECTION, "name": INJECTION, "version": INJECTION, "status": INJECTION}
        evil_plan = plan(
            INJECTION,
            workflow=evil_workflow,
            changed_fields=[INJECTION],
            added_steps=[{"id": "a", "name": INJECTION, "type": INJECTION}],
            changed_steps=[{"id": "b", "name": INJECTION, "type": INJECTION, "changes": [INJECTION]}],
            removed_steps=[{"action_id": "c", "name": INJECTION, "runs": 2, "moves_to": {"action_id": "d", "name": INJECTION}, "exits": False}],
        )
        evil_warning = {"message": f"Careful{INJECTION}", "fix": INJECTION, "path": INJECTION}
        warning = WorkflowCommand("warning", {"file": file}, f"Careful{INJECTION}\nFix: {INJECTION}")
        cases = [
            (
                "located errors",
                "check",
                400,
                {
                    "errors": [
                        {"status": f"bad{INJECTION}", "message": f"Bad{INJECTION}", "why": INJECTION, "fix": INJECTION, "path": INJECTION, "line": f"5,title=x::{INJECTION}", "column": INJECTION},
                        {"status": "invalid_value", "message": INJECTION, "why": MANY_PERCENT_SIGNS, "fix": "f", "path": None, "line": 5.0, "column": 15.0},
                    ]
                },
                1,
                [
                    WorkflowCommand("error", {"file": file, "title": f"bad{INJECTION}"}, f"Bad{INJECTION}\nWhy: {INJECTION}\nFix: {INJECTION}"),
                    WorkflowCommand("error", {"file": file, "line": "5", "col": "15", "title": "invalid_value"}, f"{INJECTION}\nWhy: {MANY_PERCENT_SIGNS}\nFix: f"),
                ],
            ),
            ("check plan", "check", 200, {"plan": evil_plan, "warnings": [evil_warning]}, 0, [warning]),
            ("apply result", "apply", 200, {"result": INJECTION, "workflow": evil_workflow, "plan": evil_plan, "warnings": [evil_warning]}, 0, [warning]),
            ("http error detail", "check", 401, {"detail": INJECTION}, 1, [WorkflowCommand("error", {"file": file}, f"PostHog answered HTTP 401: {INJECTION}")]),
            ("long http error detail", "check", 401, {"detail": LONG_DETAIL}, 1, [WorkflowCommand("error", {"file": file}, f"PostHog answered HTTP 401: {LONG_DETAIL[:1000]}")]),
            ("plan jq cannot read", "check", 200, {"plan": {**evil_plan, "changed_steps": [{"name": "x", "type": "delay", "changes": INJECTION}]}, "warnings": []}, 1, []),
        ]
        for name, mode, status, body, exit_code, expected_commands in cases:
            with self.subTest(name):
                self.server.routes.clear()
                self.server.reply(CHECK_PATH if mode == "check" else APPLY_PATH, CLEAN_FILE, status, body)

                run = self.run_script(MODE=mode)

                self.assertEqual(run.returncode, exit_code, run.stdout + run.stderr)
                log = RunnerLog(run.stdout)
                self.assertEqual(log.commands, expected_commands, run.stdout)
                self.assertFalse(log.stopped, run.stdout)
                self.assertIn("::set-output name=x::y", log.visible)
                self.assertEqual(RunnerLog(run.stderr).commands, [], run.stderr)

        with self.subTest("body without JSON"):
            self.server.reply_raw(CHECK_PATH, CLEAN_FILE, 502, f"<html>{INJECTION}</html>")

            run = self.run_script()

            log = RunnerLog(run.stdout)
            self.assertEqual([(command.name, command.properties) for command in log.commands], [("error", {"file": file})], run.stdout)
            self.assertTrue(log.commands[0].data.startswith("PostHog answered HTTP 502 without JSON: <html>"), run.stdout)
            self.assertFalse(log.stopped, run.stdout)


if __name__ == "__main__":
    unittest.main()
