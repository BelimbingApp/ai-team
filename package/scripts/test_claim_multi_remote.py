import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path

SCRIPT = Path(__file__).with_name("claim.sh")
CLAIM_BRANCH = "agent/composer-issue-42"


class ClaimMultiRemoteTest(unittest.TestCase):
    """Hermetic regressions for claim.sh: multi-remote gh inference and resume."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.bare = base / "canonical.git"
        env = self.git_env()
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        subprocess.run(
            ["git", "--git-dir", str(self.bare), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True,
            env=env,
        )

        seed = base / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=env)
        (seed / "README").write_text("base\n", encoding="utf-8")
        self.git(["add", "."], cwd=seed)
        self.git(["commit", "-q", "-m", "base"], cwd=seed)
        self.git(["remote", "add", "origin", str(self.bare)], cwd=seed)
        self.git(["push", "-q", "-u", "origin", "main"], cwd=seed)

        self.clone = base / "checkout"
        subprocess.run(
            ["git", "clone", "-q", str(self.bare), str(self.clone)],
            check=True,
            env=env,
        )
        self.assertEqual(self.git_out(["rev-parse", "--abbrev-ref", "HEAD"]), "main")

        # Second remote recreates the multi-remote layout that broke gh inference.
        self.git(["remote", "add", "fork", str(self.bare)])

        self.bin = base / "bin"
        self.bin.mkdir()
        self.gh_log = base / "gh.log"
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -euo pipefail
                log="$CLAIM_TEST_GH_LOG"
                printf '%s\\n' "$*" >>"$log"
                case "$1 $2" in
                  "repo view")
                    printf 'example/canonical\\n'
                    ;;
                  "issue view")
                    # claim.sh reads the labels back after writing them and
                    # judges the lookup by its exit status, so this has to
                    # answer --json labels --jq the way gh does (#15).
                    if printf '%s' "$*" | grep -q -- '--json labels'; then
                      printf 'agent:%s,task:active\\n' "${{CLAIM_AGENT:-}}"
                    else
                      printf '%s\\n' '{{"state":"OPEN","labels":[{{"name":"task:ready"}}],"title":"multi-remote claim","url":"https://example/issues/42"}}'
                    fi
                    ;;
                  "pr view") printf 'agent:%s,task:active\\n' "${{CLAIM_AGENT:-}}" ;;
                  "pr list")
                    printf '[]\\n'
                    ;;
                  "label list")
                    printf '[{{"name":"agent:composer"}}]\\n'
                    ;;
                  "pr create")
                    if ! printf '%s' "$*" | grep -q -- '--head'; then
                      echo 'aborted: you must first push the current branch to a remote, or use the --head flag' >&2
                      exit 1
                    fi
                    if ! printf '%s' "$*" | grep -q -- '--repo example/canonical'; then
                      echo 'missing --repo' >&2
                      exit 1
                    fi
                    body_file=""
                    prev=""
                    for arg in "$@"; do
                      if [ "$prev" = "--body-file" ]; then
                        body_file="$arg"
                      fi
                      prev="$arg"
                    done
                    if [ -z "$body_file" ] || [ ! -f "$body_file" ]; then
                      echo 'pr create missing --body-file' >&2
                      exit 1
                    fi
                    if ! grep -qE '(^|[^A-Za-z])Closes[[:space:]]+#42([^0-9]|$)' "$body_file"; then
                      echo 'claim body missing Closes #42' >&2
                      exit 1
                    fi
                    printf 'https://github.com/example/canonical/pull/99\\n'
                    ;;
                  "pr edit"|"issue edit"|"label create"|"pr view"|"pr ready")
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

    def tearDown(self):
        if self.clone.exists():
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.clone,
                capture_output=True,
                env=self.git_env(),
            )
        self.dir.cleanup()

    def git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS=os.devnull,
            GIT_AUTHOR_NAME="claim-test",
            GIT_AUTHOR_EMAIL="claim-test@example.com",
            GIT_COMMITTER_NAME="claim-test",
            GIT_COMMITTER_EMAIL="claim-test@example.com",
        )
        return env

    def git(self, args: list[str], *, cwd: Path | None = None) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd or self.clone,
            check=True,
            env=self.git_env(),
        )

    def git_out(self, args: list[str], *, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.clone,
            check=True,
            capture_output=True,
            text=True,
            env=self.git_env(),
        ).stdout.strip()

    def run_claim(self, *, worktree: Path, resume_branch: str | None = None) -> subprocess.CompletedProcess[str]:
        env = self.git_env()
        env["CLAIM_TEST_GH_LOG"] = bash_path(self.gh_log)
        env["CLAIM_AGENT"] = "composer"
        env["CLAIM_WORKTREE"] = str(worktree)
        if resume_branch:
            env["CLAIM_BRANCH"] = resume_branch
        return run_with_bash_path(
            ["bash", str(SCRIPT), "42"],
            stub_directory=self.bin,
            cwd=self.clone,
            env=env,
            capture_output=True,
            text=True,
        )

    def create_pushed_claim_branch(self, *, checkout: bool) -> str:
        """Create CLAIM_BRANCH from origin/main, empty claim commit, push. Optionally stay checked out."""
        self.git(["fetch", "-q", "origin", "main"])
        if checkout:
            self.git(["switch", "-c", CLAIM_BRANCH, "origin/main"])
        else:
            self.git(["branch", CLAIM_BRANCH, "origin/main"])
            self.git(["switch", CLAIM_BRANCH])
        self.git(["commit", "--allow-empty", "-q", "-m", "claim: #42"])
        self.git(["push", "-q", "-u", "origin", CLAIM_BRANCH])
        return CLAIM_BRANCH

    def assert_claim_success(self, result: subprocess.CompletedProcess[str], *, resumed: bool = False) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claimed #42 in draft PR #99", result.stdout)
        if resumed:
            self.assertIn("resuming #42", result.stdout)

    def assert_root_main_worktree_on_claim(self, worktree: Path) -> None:
        self.assertEqual(self.git_out(["rev-parse", "--abbrev-ref", "HEAD"]), "main")
        self.assertEqual(self.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree), CLAIM_BRANCH)

    def test_claim_passes_head_on_multi_remote_checkout(self):
        worktree = Path(self.dir.name) / "wt-fresh"
        result = self.run_claim(worktree=worktree)
        self.assert_claim_success(result)
        self.assertIn(f"worktree: {worktree}", result.stdout)
        self.assertIn("root checkout left on main", result.stdout)
        self.assertEqual(self.git_out(["rev-parse", "--abbrev-ref", "HEAD"]), "main")
        self.assertRegex(self.gh_log.read_text(encoding="utf-8"), r"pr create .*--head agent/composer-issue-42")
        self.assertRegex(
            self.gh_log.read_text(encoding="utf-8"),
            r"pr create .*--body-file",
        )

    def test_claim_body_requires_closes_keyword(self):
        """Stub rejects claim bodies without Closes #N — the mechanism under test."""
        worktree = Path(self.dir.name) / "wt-closes"
        result = self.run_claim(worktree=worktree)
        self.assert_claim_success(result)
        # The stub already failed the run if Closes #42 was missing; log proves
        # the body-file path was used rather than an inline --body that could drift.
        self.assertRegex(self.gh_log.read_text(encoding="utf-8"), r"pr create .*--body-file")

    def test_claim_without_head_would_have_failed_is_covered_by_stub(self):
        env = self.git_env()
        env["CLAIM_TEST_GH_LOG"] = bash_path(self.gh_log)
        bad = run_with_bash_path(
            ["gh", "pr", "create", "--repo", "example/canonical", "--draft", "--title", "x", "--body", "y"],
            stub_directory=self.bin,
            cwd=self.clone,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("--head flag", bad.stderr)

    def test_resume_opens_pr_when_branch_already_pushed(self):
        self.create_pushed_claim_branch(checkout=False)
        self.git(["switch", "main"])
        worktree = Path(self.dir.name) / "wt-resume"
        result = self.run_claim(worktree=worktree, resume_branch=CLAIM_BRANCH)
        self.assert_claim_success(result, resumed=True)

    def test_resume_from_root_on_abandoned_claim_branch_restores_main(self):
        """Exact post-failure state from old claim.sh: root still on claim branch."""
        self.create_pushed_claim_branch(checkout=True)
        self.assertEqual(self.git_out(["rev-parse", "--abbrev-ref", "HEAD"]), CLAIM_BRANCH)

        worktree = Path(self.dir.name) / "wt-abandoned-root"
        result = self.run_claim(worktree=worktree, resume_branch=CLAIM_BRANCH)
        self.assert_claim_success(result, resumed=True)
        self.assert_root_main_worktree_on_claim(worktree)
        self.assertIn("root checkout left on main", result.stdout)

    def test_resume_repairs_existing_detached_worktree_and_root_on_claim(self):
        """Legacy half-claim: root on claim branch AND detached worktree already present."""
        self.create_pushed_claim_branch(checkout=True)
        worktree = Path(self.dir.name) / "wt-detached-existing"
        self.git(["worktree", "add", "--detach", str(worktree), "HEAD"])
        self.assertEqual(self.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree), "HEAD")

        result = self.run_claim(worktree=worktree, resume_branch=CLAIM_BRANCH)
        self.assert_claim_success(result, resumed=True)
        self.assert_root_main_worktree_on_claim(worktree)

    def test_resume_preserves_unpushed_local_commits_on_existing_worktree(self):
        """Do not force-reset local half-claim commits onto origin when repairing."""
        self.create_pushed_claim_branch(checkout=True)
        self.git(["commit", "--allow-empty", "-q", "-m", "local-only sentinel"])
        sentinel = self.git_out(["rev-parse", "HEAD"])
        pushed = self.git_out(["rev-parse", f"origin/{CLAIM_BRANCH}"])
        self.assertNotEqual(sentinel, pushed)

        worktree = Path(self.dir.name) / "wt-unpushed-local"
        self.git(["worktree", "add", "--detach", str(worktree), pushed])

        result = self.run_claim(worktree=worktree, resume_branch=CLAIM_BRANCH)
        self.assert_claim_success(result, resumed=True)
        self.assert_root_main_worktree_on_claim(worktree)
        self.assertEqual(self.git_out(["rev-parse", "HEAD"], cwd=worktree), sentinel)


if __name__ == "__main__":
    unittest.main()
