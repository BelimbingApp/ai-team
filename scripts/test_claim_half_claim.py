import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import run_with_bash_path

SCRIPT = Path(__file__).with_name("claim.sh")


class ClaimHalfClaimTest(unittest.TestCase):
    """#15: claim.sh created a PR and stopped before the labels landed.

    The issue kept reading task:ready while a lane existed for it — the one
    state worse than no claim at all, because the next agent claims it and
    collides, and the PR cannot pass gate.sh's "exactly one agent lane".
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        env = self.git_env()

        self.bare = base / "canonical.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        subprocess.run(
            ["git", "--git-dir", str(self.bare), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True, env=env,
        )
        seed = base / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=env)
        (seed / "README").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=seed, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=seed, check=True, env=env)
        subprocess.run(["git", "remote", "add", "origin", str(self.bare)], cwd=seed, check=True, env=env)
        subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=seed, check=True, env=env)

        self.clone = base / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.clone)], check=True, env=env)

        self.bin = base / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        # The readback is what makes the write verified, so the stub has to
        # model it: `issue view --json labels` answers with the *post-write*
        # label set, which the tests drive independently of the pre-flight one.
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                case "$1 $2" in
                  "repo view") printf 'example/canonical\\n' ;;
                  "issue view")
                    if printf '%s' "$*" | grep -q -- '--json labels'; then
                      printf '%s\\n' "${CLAIM_TEST_ISSUE_READBACK:-}"
                    else
                      printf '%s\\n' "$CLAIM_TEST_ISSUE_JSON"
                    fi
                    ;;
                  "pr view") printf '%s\\n' "${CLAIM_TEST_PR_READBACK:-}" ;;
                  "pr list") printf '%s\\n' "${CLAIM_TEST_PR_LIST:-[]}" ;;
                  "label list") printf '[{"name":"agent:fable"},{"name":"agent:opus-5"}]\\n' ;;
                  "pr create") printf 'https://example/pull/99\\n' ;;
                  "pr edit") echo "$*" >>"${CLAIM_TEST_EDITS:-/dev/null}"; exit 0 ;;
                  "issue edit") echo "$*" >>"${CLAIM_TEST_EDITS:-/dev/null}"; exit 0 ;;
                  *) exit 1 ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.edits = self.bin / "edits"

    def git_env(self):
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        })
        return env

    def run_claim(self, *, agent="opus-5", pr_list="[]", issue_labels=("task:ready",),
                  pr_readback="", issue_readback=""):
        env = self.git_env()
        env["CLAIM_AGENT"] = agent
        env["CLAIM_TEST_ISSUE_JSON"] = json.dumps({
            "state": "OPEN",
            "labels": [{"name": name} for name in issue_labels],
            "title": "a task",
            "url": "https://example/issues/42",
        })
        env["CLAIM_TEST_PR_LIST"] = pr_list
        env["CLAIM_TEST_PR_READBACK"] = pr_readback
        env["CLAIM_TEST_ISSUE_READBACK"] = issue_readback
        env["CLAIM_TEST_EDITS"] = str(self.edits)
        return run_with_bash_path(
            ["bash", str(SCRIPT), "42"],
            stub_directory=self.bin,
            env=env, cwd=self.clone, text=True, capture_output=True, check=False,
        )

    @staticmethod
    def half_claim(marker="opus-5", number=14):
        """An open PR for #42 with a From: marker and no agent:* label."""
        return json.dumps([{
            "number": number,
            "title": "a task (#42)",
            "body": f"**From:** {marker}\n\n**Reachable:** board\n\nCloses #42\n",
            "headRefName": f"agent/{marker}-issue-42",
            "labels": [],
            "url": f"https://example/pull/{number}",
        }])

    def test_your_own_half_claim_is_finished_rather_than_refused(self):
        # Re-running claim.sh is what an agent actually tries after a failed
        # run; before this it hit "an open PR already holds it" and stopped.
        result = self.run_claim(
            pr_list=self.half_claim(),
            pr_readback="agent:opus-5,task:active",
            issue_readback="agent:opus-5,task:active",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("your own half-claim", result.stdout)
        edits = self.edits.read_text(encoding="utf-8")
        self.assertIn("pr edit 14", edits)
        self.assertIn("--add-label agent:opus-5", edits)
        self.assertIn("--add-label task:active", edits)
        # A repair must not open a second lane for the same issue.
        self.assertNotIn("pr create", edits)

    def test_someone_elses_half_claim_is_refused_but_named_and_repairable(self):
        result = self.run_claim(agent="opus-5", pr_list=self.half_claim(marker="gpt-5"))

        self.assertEqual(result.returncode, 1)
        self.assertIn("HALF-CLAIM by gpt-5", result.stderr)
        self.assertIn("still looks free to everyone else", result.stderr)
        self.assertIn("gh pr edit 14", result.stderr)
        self.assertIn("--add-label agent:gpt-5", result.stderr)

    def test_an_unnamed_half_claim_is_still_reported_as_one(self):
        anonymous = json.dumps([{
            "number": 14, "title": "a task (#42)", "body": "no marker here",
            "headRefName": "agent/mystery-issue-42", "labels": [],
            "url": "https://example/pull/14",
        }])

        result = self.run_claim(pr_list=anonymous)

        self.assertEqual(result.returncode, 1)
        self.assertIn("HALF-CLAIM by an unnamed agent", result.stderr)
        self.assertIn("agent:<owner>", result.stderr)

    def test_a_label_that_does_not_stick_fails_the_claim_loudly(self):
        # The tail path: the PR was created, the labels were attempted, and the
        # readback proves one never landed. Silence here is exactly #15.
        result = self.run_claim(
            pr_readback="task:active",
            issue_readback="agent:opus-5,task:active",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("HALF-CLAIM", result.stderr)
        self.assertIn("agent:opus-5", result.stderr)
        self.assertIn("another agent can collide", result.stderr)

    def test_an_unreadable_readback_warns_and_leaves_the_claim_standing(self):
        # gh unavailable is not evidence that a label is missing; the claim
        # succeeded and the agent is told how to check it.
        result = self.run_claim(pr_readback="", issue_readback="")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not read back the labels", result.stderr)
        self.assertIn("claimed #42", result.stdout)


if __name__ == "__main__":
    unittest.main()
