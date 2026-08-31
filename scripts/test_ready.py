import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path

SCRIPT = Path(__file__).with_name("ready.sh")
LANE = Path(__file__).with_name("_lane_issue.sh")


class ReadyHandoffTest(unittest.TestCase):
    """Hermetic regressions for ready.sh: Closes keyword re-assert + label handoff."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.body_path = base / "pr-body.txt"
        self.body_path.write_text(
            "**From:** composer\n\nImplementation notes only — keyword stripped.\n",
            encoding="utf-8",
        )
        self.title = "claim body closes (#42)"
        self.branch = "agent/composer-issue-42"
        self.bin = base / "bin"
        self.bin.mkdir()
        self.gh_log = base / "gh.log"
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -euo pipefail
                log="$READY_TEST_GH_LOG"
                body_path="$READY_TEST_BODY"
                printf '%s\\n' "$*" >>"$log"
                case "$1 $2" in
                  "repo view")
                    printf 'example/canonical\\n'
                    ;;
                  "pr view")
                    body=$(cat "$body_path")
                    jq -n --arg body "$body" --arg title "$READY_TEST_TITLE" --arg branch "$READY_TEST_BRANCH" '{{
                      number: 99,
                      title: $title,
                      body: $body,
                      headRefName: $branch,
                      labels: [{{"name":"agent:composer"}},{{"name":"task:active"}}],
                      isDraft: true,
                      state: "OPEN"
                    }}'
                    ;;
                  "pr edit")
                    prev=""
                    for arg in "$@"; do
                      if [ "$prev" = "--body-file" ]; then
                        cp "$arg" "$body_path"
                      fi
                      prev="$arg"
                    done
                    ;;
                  "pr ready"|"issue edit")
                    ;;
                  *)
                    echo "unexpected gh: $*" >&2
                    exit 1
                    ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.cwd = base

    def tearDown(self):
        self.dir.cleanup()

    def run_ready(self, *, ready_issue: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["READY_TEST_GH_LOG"] = bash_path(self.gh_log)
        env["READY_TEST_BODY"] = bash_path(self.body_path)
        env["READY_TEST_TITLE"] = self.title
        env["READY_TEST_BRANCH"] = self.branch
        env["CLAIM_AGENT"] = "composer"
        env["AI_TEAM_TEST_ORIGIN_REPO"] = "example/canonical"
        if ready_issue is not None:
            env["READY_ISSUE"] = ready_issue
        elif "READY_ISSUE" in env:
            del env["READY_ISSUE"]
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return run_with_bash_path(
            ["bash", bash_path(SCRIPT), "99"],
            stub_directory=self.bin,
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_ready_reasserts_closes_when_body_lost_it(self):
        result = self.run_ready()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("re-asserted Closes #42", result.stdout)
        self.assertIn("PR #99 ready for review (Closes #42)", result.stdout)
        body = self.body_path.read_text(encoding="utf-8")
        self.assertRegex(body, r"(?m)^Closes #42\s*$")
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("pr ready", log)
        self.assertIn("task:review", log)

    def test_ready_skips_rewrite_when_closes_already_present(self):
        self.body_path.write_text(
            "**From:** composer\n\nClaiming #42 through claim.sh.\n\nCloses #42\n",
            encoding="utf-8",
        )
        result = self.run_ready()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("re-asserted", result.stdout)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotRegex(log, r"pr edit .*--body-file")

    def test_ready_refuses_conflicting_title_and_branch(self):
        self.title = "renamed lane (#999)"
        self.branch = "agent/composer-issue-42"
        result = self.run_ready()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("disagrees", result.stderr)
        self.assertNotIn("Closes #999", self.body_path.read_text(encoding="utf-8"))

    def test_ready_uses_trailing_title_marker_not_earlier_ones(self):
        self.title = "Backport context (#99) for lane (#42)"
        self.branch = "agent/composer-issue-42"
        result = self.run_ready()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Closes #42", result.stdout)

    def test_ready_issue_override_must_agree(self):
        result = self.run_ready(ready_issue="99")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("READY_ISSUE", result.stderr)

    def test_ready_issue_override_fills_underivable_lane(self):
        self.title = "Ad-hoc change"
        self.branch = "agent/composer-misc"
        result = self.run_ready(ready_issue="42")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Closes #42", result.stdout)


class LaneIssueHelperTest(unittest.TestCase):
    """Source-level probes for _lane_issue.sh via the shared Windows Bash resolver."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.stub = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def derive(self, title: str, branch: str, body: str = "", override: str = "") -> str:
        result = run_with_bash_path(
            [
                "bash",
                "-c",
                f'source "{bash_path(LANE)}"; ai_team_derive_lane_issue "$1" "$2" "$3" "$4"',
                "_",
                title,
                branch,
                body,
                override,
            ],
            stub_directory=self.stub,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_issue_less_marker(self):
        self.assertEqual(
            self.derive("Ad-hoc", "agent/author-misc", "AI-Team-Lane-Issue: none\n"),
            "none",
        )

    def test_conflict(self):
        out = self.derive("x (#1)", "agent/a-issue-2")
        self.assertTrue(out.startswith("error:"), out)


if __name__ == "__main__":
    unittest.main()
