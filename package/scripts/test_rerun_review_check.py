import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


SCRIPT = Path(__file__).with_name("rerun-review-check.sh")
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


class RerunReviewCheckTest(unittest.TestCase):
    """The rerun helper may only replay an existing trusted exact-head run."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)], check=True)
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Rerun Test")
        (self.work / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "fixture")

        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.calls = self.base / "gh-calls.txt"
        self.identity = self.base / "identity.json"
        self.runs = self.base / "runs.json"
        self.write_identity()
        self.write_runs()
        self.write_gh()

        self.env = os.environ.copy()
        self.env["AI_TEAM_TEST_ORIGIN_REPO"] = "example/project"
        self.env["GH_CALLS"] = bash_path(self.calls)
        self.env["IDENTITY_JSON"] = bash_path(self.identity)
        self.env["RUNS_JSON"] = bash_path(self.runs)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.work, check=True, capture_output=True)

    def write_identity(self, *, head=HEAD):
        self.identity.write_text(
            json.dumps(
                {
                    "number": 7,
                    "draft": False,
                    "head": {"sha": head, "ref": "agent/example-issue-7"},
                    "base": {"ref": "main"},
                }
            ),
            encoding="utf-8",
        )

    def write_runs(self, runs=None):
        if runs is None:
            runs = [
                {
                    "id": 100,
                    "name": "mechanisms",
                    "event": "pull_request_target",
                    "head_sha": HEAD,
                    "head_branch": "agent/example-issue-7",
                    "pull_requests": [{"number": 7}],
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": "2026-09-01T00:01:00Z",
                },
                {
                    "id": 101,
                    "name": "independent review",
                    "event": "pull_request",
                    "head_sha": HEAD,
                    "head_branch": "agent/example-issue-7",
                    "pull_requests": [{"number": 7}],
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-09-01T00:02:00Z",
                },
                {
                    "id": 102,
                    "name": "independent review",
                    "event": "pull_request_target",
                    "head_sha": OTHER_HEAD,
                    "head_branch": "agent/example-issue-7",
                    "pull_requests": [{"number": 7}],
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-09-01T00:03:00Z",
                },
                {
                    "id": 103,
                    "name": "independent review",
                    "event": "pull_request_target",
                    "head_sha": HEAD,
                    "head_branch": "agent/example-issue-7",
                    "pull_requests": [{"number": 99}],
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-09-01T00:04:00Z",
                },
                {
                    "id": 104,
                    "name": "independent review",
                    "event": "pull_request_target",
                    "head_sha": HEAD,
                    "head_branch": "agent/example-issue-7",
                    "pull_requests": [{"number": 7}],
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-09-01T00:05:00Z",
                    "html_url": "https://github.com/example/project/actions/runs/104",
                },
            ]
        self.runs.write_text(json.dumps({"workflow_runs": runs}), encoding="utf-8")

    def write_gh(self):
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\n' "$*" >> "$GH_CALLS"
                case "${1:-} ${2:-}" in
                  "api"*)
                    case "$*" in
                      *"pulls/7"*) cat "$IDENTITY_JSON" ;;
                      *"actions/runs?event=pull_request_target"*) cat "$RUNS_JSON" ;;
                      *) printf 'unexpected api path: %s\n' "$*" >&2; exit 90 ;;
                    esac
                    ;;
                  "run rerun")
                    [ "${3:-}" = "104" ] || exit 91
                    ;;
                  *) printf 'unexpected gh call: %s\n' "$*" >&2; exit 92 ;;
                esac
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def run_script(self):
        return run_with_bash_path(
            ["bash", bash_path(SCRIPT), "7"],
            stub_directory=self.bin,
            env=self.env,
            cwd=self.work,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_reruns_latest_matching_trusted_run(self):
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reran Independent review run 104", result.stdout)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn("event=pull_request_target&head_sha=" + HEAD, calls)
        self.assertIn("run rerun 104 --repo example/project", calls)
        self.assertNotIn("run rerun 103", calls)

    def test_no_matching_current_head_fails_without_rerun(self):
        self.write_runs(
            [
                {
                    "id": 200,
                    "name": "independent review",
                    "event": "pull_request_target",
                    "head_sha": OTHER_HEAD,
                    "head_branch": "agent/example-issue-7",
                    "pull_requests": [{"number": 7}],
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-09-01T00:05:00Z",
                }
            ]
        )

        result = self.run_script()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no existing trusted Independent review run", result.stderr)
        self.assertNotIn("run rerun", self.calls.read_text(encoding="utf-8"))

    def test_head_change_between_reads_refuses_stale_run(self):
        self.env["OTHER_HEAD"] = OTHER_HEAD
        self.write_gh_with_head_race()

        result = self.run_script()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("moved from head", result.stderr)
        self.assertNotIn("run rerun", self.calls.read_text(encoding="utf-8"))

    def write_gh_with_head_race(self):
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\n' "$*" >> "$GH_CALLS"
                case "${1:-} ${2:-}" in
                  "api"*)
                    case "$*" in
                      *"pulls/7"*)
                        if [ ! -e "$IDENTITY_READS_FILE" ]; then
                          : > "$IDENTITY_READS_FILE"
                          cat "$IDENTITY_JSON"
                        else
                          printf '{"head":{"sha":"%s","ref":"agent/example-issue-7"}}\n' "$OTHER_HEAD"
                        fi
                        ;;
                      *"actions/runs?event=pull_request_target"*) cat "$RUNS_JSON" ;;
                      *) exit 90 ;;
                    esac
                    ;;
                  "run rerun") exit 91 ;;
                  *) exit 92 ;;
                esac
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.env["IDENTITY_READS_FILE"] = bash_path(self.base / "identity-reads")


if __name__ == "__main__":
    unittest.main()
