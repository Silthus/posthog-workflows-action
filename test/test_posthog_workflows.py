import json
import os
import subprocess
import tempfile
import threading
import unittest
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


class MockPostHog(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.routes: dict[tuple[str, str], tuple[int, str, str]] = {}
        self.requests: list[dict] = []

    def reply(self, path: str, content: str, status: int, body: object) -> None:
        self.routes[(path, content)] = (status, "application/json", json.dumps(body))

    def reply_raw(self, path: str, content: str, status: int, body: str) -> None:
        self.routes[(path, content)] = (status, "text/html", body)


class MockHandler(BaseHTTPRequestHandler):
    server: MockPostHog

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "authorization": self.headers["Authorization"], "content_type": self.headers["Content-Type"], "body": body}
        )
        status, content_type, payload = self.server.routes.get((self.path, body.get("content")), (500, "text/plain", "no route"))
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

        run = self.run_script()

        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
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

        run = self.run_script(POSTHOG_API_KEY="")

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(len(run.stdout.splitlines()), 1, run.stdout)
        self.assertIn("No PostHog API key", run.stdout)
        self.assertEqual(run.stderr, "")
        self.assertEqual(self.server.requests, [])

    def test_a_non_json_server_error_prints_a_readable_message_and_fails(self) -> None:
        self.write("trial.yaml", CLEAN_FILE)
        self.server.reply_raw(CHECK_PATH, CLEAN_FILE, 502, "<html><body>Bad gateway</body></html>")

        run = self.run_script()

        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("workflows/trial.yaml: PostHog answered HTTP 502 without JSON: <html><body>Bad gateway</body></html>", run.stdout)
        self.assertIn("::error file=workflows/trial.yaml::PostHog answered HTTP 502 without JSON", run.stdout)


if __name__ == "__main__":
    unittest.main()
