import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path

SCRIPT = Path(__file__).parent / "cleanup.sh"


class CleanupWorktreeTest(unittest.TestCase):
    """cleanup.sh --yes removes a worktree only when it is clean and its HEAD
    is already on origin; unpushed work and dirty trees are kept and named."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.bare = base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        self.clone = base / "clone"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.clone)], check=True, env=self.env())
        (self.clone / "f.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.git("remote", "add", "origin", str(self.bare))
        self.git("push", "-q", "-u", "origin", "main")
        (Path(self.dir.name) / "stubs").mkdir()
        gh = Path(self.dir.name) / "stubs" / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                case "$1 $2" in
                  "repo view") printf 'example/canonical\\n' ;;
                  "pr list")
                    [ "${CLEANUP_TEST_PR_LIST_FAIL:-}" != "1" ] || exit 1
                    printf '%s\\n' "${CLEANUP_TEST_OPEN_PRS:-[]}"
                    ;;
                  *) echo "unexpected gh: $*" >&2; exit 1 ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.dir.cleanup()

    def env(self):
        env = os.environ.copy()
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
        )
        return env

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.clone, check=True, env=self.env(),
            capture_output=True, text=True,
        ).stdout.strip()

    def run_cleanup(self, *args, open_prs="[]", pr_list_fails=False):
        env = self.env()
        env["AI_TEAM_TEST_ORIGIN_REPO"] = "example/canonical"
        env["CLEANUP_TEST_OPEN_PRS"] = open_prs
        if pr_list_fails:
            env["CLEANUP_TEST_PR_LIST_FAIL"] = "1"
        return run_with_bash_path(
            ["bash", bash_path(SCRIPT), *args],
            stub_directory=Path(self.dir.name) / "stubs",
            cwd=self.clone,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_yes_removes_landed_worktree_and_keeps_unpushed_one(self):
        landed = Path(self.dir.name) / "wt-landed"
        self.git("worktree", "add", "-q", "-b", "agent/a-issue-1", str(landed), "origin/main")
        (landed / "f.txt").write_text("landed\n")
        self.git("commit", "-q", "-am", "lane work", cwd=landed)
        self.git("push", "-q", "origin", "agent/a-issue-1:main", cwd=landed)

        unpushed = Path(self.dir.name) / "wt-unpushed"
        self.git("worktree", "add", "-q", "-b", "agent/a-issue-2", str(unpushed), "origin/main")
        self.git("commit", "-q", "--allow-empty", "-m", "not pushed", cwd=unpushed)

        dirty = Path(self.dir.name) / "wt-dirty"
        self.git("worktree", "add", "-q", "--detach", str(dirty), "origin/main")
        (dirty / "scratch.txt").write_text("uncommitted\n")

        dry = self.run_cleanup()
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn(f"would remove {landed}", dry.stdout)
        self.assertTrue(landed.is_dir(), "a dry run must remove nothing")

        result = self.run_cleanup("--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"removed {landed}", result.stdout)
        self.assertIn("deleted agent/a-issue-1", result.stdout)
        self.assertFalse(landed.exists())
        self.assertIn(f"kept {unpushed}", result.stdout)
        self.assertIn("unpushed work", result.stdout)
        self.assertTrue(unpushed.is_dir())
        self.assertIn(f"kept {dirty}", result.stdout)
        self.assertIn("uncommitted changes", result.stdout)
        self.assertTrue(dirty.is_dir())
        branches = self.git("branch", "--format=%(refname:short)")
        self.assertNotIn("agent/a-issue-1", branches)
        self.assertIn("agent/a-issue-2", branches)

    def test_yes_preserves_clean_pushed_worktree_for_open_pr(self):
        active = Path(self.dir.name) / "wt-active"
        branch = "agent/a-issue-42"
        self.git("worktree", "add", "-q", "-b", branch, str(active), "origin/main")
        self.git("commit", "-q", "--allow-empty", "-m", "active lane", cwd=active)
        self.git("push", "-q", "-u", "origin", branch, cwd=active)

        result = self.run_cleanup(
            "--yes",
            open_prs='[{"number":99,"headRefName":"agent/a-issue-42"}]',
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"kept {active} [{branch}] — open PR #99", result.stdout)
        self.assertTrue(active.is_dir(), "an open PR's worktree is active, not cleanup garbage")

    def test_yes_preserves_clean_pushed_worktree_when_open_pr_registry_is_unavailable(self):
        uncertain = Path(self.dir.name) / "wt-uncertain"
        branch = "agent/a-issue-43"
        self.git("worktree", "add", "-q", "-b", branch, str(uncertain), "origin/main")
        self.git("commit", "-q", "--allow-empty", "-m", "uncertain lane", cwd=uncertain)
        self.git("push", "-q", "-u", "origin", branch, cwd=uncertain)

        result = self.run_cleanup("--yes", pr_list_fails=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("open-PR state is unavailable", result.stdout)
        self.assertTrue(uncertain.is_dir(), "uncertainty must preserve state")

    def test_yes_preserves_worktree_when_registry_has_duplicate_branch_matches(self):
        ambiguous = Path(self.dir.name) / "wt-ambiguous"
        branch = "agent/a-issue-44"
        self.git("worktree", "add", "-q", "-b", branch, str(ambiguous), "origin/main")
        self.git("commit", "-q", "--allow-empty", "-m", "ambiguous lane", cwd=ambiguous)
        self.git("push", "-q", "-u", "origin", branch, cwd=ambiguous)

        result = self.run_cleanup(
            "--yes",
            open_prs=(
                '[{"number":100,"headRefName":"agent/a-issue-44"},'
                '{"number":101,"headRefName":"agent/a-issue-44"}]'
            ),
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("open PR #100, #101", result.stdout)
        self.assertTrue(ambiguous.is_dir(), "ambiguous registry state must be preserved")


if __name__ == "__main__":
    unittest.main()
