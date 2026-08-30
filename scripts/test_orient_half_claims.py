import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import run_with_bash_path


class OrientHalfClaimTest(unittest.TestCase):
    """#19: an open PR with no agent:* label whose issue still reads unclaimed
    is a lane the board is not showing. Both halves of that evidence were
    already on the orientation page and nothing joined them, so a lane sat
    invisible until a steward noticed it by eye."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)
        self.scripts = Path(__file__).parent

        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        })
        self.git_env = env
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.base)], check=True)
        (self.base / "README").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.base, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.base, check=True, env=env)

        self.bin = self.base / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                case "$1 $2" in
                  "repo view") printf 'example/canonical\\n' ;;
                  "issue list")
                    # halt_status.sh reads emptiness, not '[]', as "no halt".
                    if [[ "$*" == *"--label ops:halt"* ]]; then
                      printf ''
                    else
                      printf '[]\\n'
                    fi
                    ;;
                  "pr list")
                    if [[ "$*" == *"--json number,labels"* ]]; then
                      printf '%s\\n' "${ORIENT_TEST_UNLABELLED_PRS:-[]}"
                    else
                      printf '[]\\n'
                    fi
                    ;;
                  "pr view") printf '%s\\n' "${ORIENT_TEST_PR_DETAIL:-{}}" ;;
                  "issue view")
                    # Two different --jq filters run against the same call;
                    # answer each with what jq would have printed.
                    if [[ "$*" == *'startswith("agent:")'* ]]; then
                      printf '%s\\n' "${ORIENT_TEST_ISSUE_AGENTS:-0}"
                    else
                      printf '%s\\n' "${ORIENT_TEST_ISSUE_STATE:-task:ready}"
                    fi
                    ;;
                  *) printf '[]\\n' ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def run_orient(self, *, unlabelled="", detail=None, issue_agents="0", issue_state="task:ready"):
        env = self.git_env.copy()
        env.update(
            AI_TEAM_BASE_BRANCH="main",
            ORIENT_TEST_UNLABELLED_PRS=unlabelled,
            ORIENT_TEST_ISSUE_AGENTS=issue_agents,
            ORIENT_TEST_ISSUE_STATE=issue_state,
            ORIENT_TEST_PR_DETAIL=json.dumps(detail or {}),
            PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
        )
        return run_with_bash_path(
            ["bash", str(self.scripts / "orient.sh")],
            stub_directory=self.bin, cwd=self.base, env=env,
            capture_output=True, text=True, check=False,
        )

    def section(self, output: str) -> str:
        marker = "== half-claims"
        self.assertIn(marker, output)
        rest = output.split(marker, 1)[1]
        return rest.split("\n== ", 1)[0]

    def test_a_half_claim_is_reported_with_its_owner_and_repair_commands(self):
        result = self.run_orient(
            unlabelled="1",
            detail={
                "title": "gate.sh expects checks a pull request can never run (#1)",
                "body": "**From:** gpt-5\n\n**Reachable:** board\n\nCloses #1\n",
                "headRefName": "agent/gpt-5-issue-1",
            },
            # The issue carries no agent:* label — the board reads it as free.
            issue_agents="0",
        )

        section = self.section(result.stdout)
        self.assertIn("#1 holds #1", section)
        self.assertIn("gh pr edit 1 --add-label agent:gpt-5", section)
        self.assertIn("gh issue edit 1 --add-label agent:gpt-5", section)

    def test_an_unlabelled_pr_whose_issue_is_already_claimed_is_not_reported(self):
        # The PR may simply be missing a label; the issue names an owner, so
        # the board is not lying and this is not the failure #19 is about.
        result = self.run_orient(
            unlabelled="1",
            detail={
                "title": "a task (#1)",
                "body": "**From:** gpt-5\n\nCloses #1\n",
                "headRefName": "agent/gpt-5-issue-1",
            },
            issue_agents="1",
        )

        self.assertIn("none", self.section(result.stdout))

    def test_a_pr_with_no_derivable_lane_issue_is_not_reported(self):
        # Refusing to guess is the point: an unrelated branch must not be
        # announced as a half-claim on some issue it never referenced.
        result = self.run_orient(
            unlabelled="7",
            detail={
                "title": "drive-by docs fix",
                "body": "no marker, no reference",
                "headRefName": "docs/typo",
            },
        )

        self.assertIn("none", self.section(result.stdout))

    def test_a_half_claim_without_a_marker_says_to_ask_rather_than_guess(self):
        result = self.run_orient(
            unlabelled="1",
            detail={
                "title": "a task (#1)",
                "body": "no marker here",
                "headRefName": "agent/mystery-issue-1",
            },
        )

        section = self.section(result.stdout)
        self.assertIn("#1 holds #1", section)
        self.assertIn("ask on the lane before labelling it", section)
        self.assertNotIn("--add-label agent:", section)

    def test_two_distinct_markers_are_not_resolved_to_the_first_one(self):
        # The identity grammar is gate.sh's: collect, unique, and require
        # exactly one. Taking the first match would hand the lane to whichever
        # marker appeared earliest in a concatenated or forged body — the
        # ownership guess this section exists to refuse.
        result = self.run_orient(
            unlabelled="1",
            detail={
                "title": "a task (#1)",
                "body": "**From:** gpt-5\n\nquoted from elsewhere:\n\n**From:** claude-opus-5\n",
                "headRefName": "agent/gpt-5-issue-1",
            },
        )

        section = self.section(result.stdout)
        self.assertIn("#1 holds #1", section)
        self.assertIn("ask on the lane before labelling it", section)
        self.assertNotIn("--add-label agent:", section)

    def test_a_repeated_identical_marker_is_still_one_identity(self):
        # unique, not count: the same agent quoting their own claim line does
        # not make the owner ambiguous.
        result = self.run_orient(
            unlabelled="1",
            detail={
                "title": "a task (#1)",
                "body": "**From:** gpt-5\n\n**From:** gpt-5\n",
                "headRefName": "agent/gpt-5-issue-1",
            },
        )

        section = self.section(result.stdout)
        self.assertIn("gh pr edit 1 --add-label agent:gpt-5", section)

    def test_a_clean_board_says_none(self):
        self.assertIn("none", self.section(self.run_orient().stdout))


if __name__ == "__main__":
    unittest.main()
