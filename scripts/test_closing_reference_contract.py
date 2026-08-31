import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import test_gate_preflight
import test_ready
from _test_support import bash_path, run_with_bash_path


SCRIPTS = Path(__file__).parent
SHARED = SCRIPTS / "_lane_issue.sh"
CALLERS = (SCRIPTS / "gate.sh", SCRIPTS / "ready.sh", SCRIPTS / "orient.sh")
CLOSING_KEYWORDS = ("Closes", "Fixes", "Resolves", "Fixed", "Close")


class OrientHarness:
    """Run orient.sh with a single review lane and no network."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.root = base / "repo"
        self.scripts = self.root / "docs" / "ai-team" / "scripts"
        self.bin = base / "bin"
        self.scripts.mkdir(parents=True)
        self.bin.mkdir()

        git_env = os.environ.copy()
        git_env.update(
            GIT_AUTHOR_NAME="test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True, env=git_env)
        (self.root / "fixture.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "fixture.txt"], cwd=self.root, check=True, env=git_env)
        subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=self.root, check=True, env=git_env)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            env=git_env, text=True, capture_output=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head],
            cwd=self.root, check=True, env=git_env,
        )

        for name in ("orient.sh", "_lane_issue.sh", "_default_branch.sh", "halt_status.sh", "board.sh"):
            target = self.scripts / name
            shutil.copy2(SCRIPTS / name, target)
            target.chmod(target.stat().st_mode | stat.S_IXUSR)

        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                if [ "$1 $2" = "repo view" ]; then
                  printf 'example/canonical\\n'
                elif [ "$1 $2" = "pr list" ] && [[ " $* " == *" --label task:review "* ]]; then
                  printf '%s\\n' "$ORIENT_TEST_PRS"
                fi
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def cleanup(self):
        self.directory.cleanup()

    def run(self, body: str) -> object:
        fixture = [{
            "number": 99,
            "title": "Closing contract (#42)",
            "headRefName": "agent/author-issue-42",
            "body": body,
        }]
        env = os.environ.copy()
        env["AI_TEAM_TEST_ORIGIN_REPO"] = "example/canonical"
        env["ORIENT_TEST_PRS"] = json.dumps(fixture)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return run_with_bash_path(
            ["bash", bash_path(self.scripts / "orient.sh")],
            stub_directory=self.bin,
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


class ClosingReferenceContractTest(unittest.TestCase):
    """Round-trip the shared predicate through gate, ready, and orient."""

    def setUp(self):
        self.gate = test_gate_preflight.GateMechanismTest()
        self.gate.setUp()
        self.addCleanup(self.gate.tearDown)

        self.ready = test_ready.ReadyHandoffTest()
        self.ready.setUp()
        self.addCleanup(self.ready.tearDown)

        self.orient = OrientHarness()
        self.addCleanup(self.orient.cleanup)

    def test_callers_delegate_to_one_shared_definition(self):
        for caller in CALLERS:
            source = caller.read_text(encoding="utf-8")
            with self.subTest(caller=caller.name):
                self.assertIn('source "$here/_lane_issue.sh"' if caller.name != "orient.sh"
                              else 'source "$SCRIPT_DIR/_lane_issue.sh"', source)
                self.assertEqual(source.count("ai_team_body_has_closing_reference"), 1)
                self.assertNotIn("close[sd]?", source)

        shared = SHARED.read_text(encoding="utf-8")
        self.assertEqual(shared.count("close[sd]?"), 1)

    def test_all_keywords_round_trip_through_gate_ready_and_orient(self):
        for keyword in CLOSING_KEYWORDS:
            body = f"**From:** author\n\n{keyword} #42\n"
            with self.subTest(keyword=keyword):
                gate = self.gate.run_gate(
                    origin=test_gate_preflight.CANONICAL_HTTPS,
                    reviewed=self.gate.head_sha,
                    body=body,
                )
                self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)

                self.ready.body_path.write_text(body, encoding="utf-8")
                ready = self.ready.run_ready()
                self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
                self.assertNotIn("re-asserted", ready.stdout)
                self.assertEqual(self.ready.body_path.read_text(encoding="utf-8"), body)

                orient = self.orient.run(body)
                self.assertEqual(orient.returncode, 0, orient.stdout + orient.stderr)
                self.assertIn("every task:review lane satisfies", orient.stdout)
                self.assertNotIn("has no closing reference", orient.stdout)

    def test_wrong_issue_is_rejected_repaired_and_reported_for_same_lane(self):
        body = "**From:** author\n\nFixes #99\n"

        gate = self.gate.run_gate(
            origin=test_gate_preflight.CANONICAL_HTTPS,
            reviewed=self.gate.head_sha,
            body=body,
        )
        self.assertEqual(gate.returncode, 1, gate.stdout + gate.stderr)
        self.assertIn("body has no closing reference to #42", gate.stdout)

        self.ready.body_path.write_text(body, encoding="utf-8")
        ready = self.ready.run_ready()
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertIn("re-asserted Closes #42", ready.stdout)
        self.assertIn("Closes #42", self.ready.body_path.read_text(encoding="utf-8"))

        orient = self.orient.run(body)
        self.assertEqual(orient.returncode, 0, orient.stdout + orient.stderr)
        self.assertIn("#99 has no closing reference to #42", orient.stdout)


if __name__ == "__main__":
    unittest.main()
