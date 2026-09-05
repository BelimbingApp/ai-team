import os
import shutil
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
                  "issue list")
                    if [ "${{CLAIM_TEST_HALT:-}}" = "1" ]; then
                      printf '  HALT #8 — maintenance\\n'
                    fi
                    ;;
                  "issue view")
                    # claim.sh reads the labels back after writing them and
                    # judges the lookup by its exit status, so this has to
                    # answer --json labels --jq the way gh does (#15).
                    if printf '%s' "$*" | grep -q -- '--json labels'; then
                      if [ "${{CLAIM_TEST_LEAVE_READY:-}}" = "1" ]; then
                        printf 'agent:%s,task:active,task:ready\\n' "${{CLAIM_AGENT:-}}"
                      else
                        printf 'agent:%s,task:active\\n' "${{CLAIM_AGENT:-}}"
                      fi
                    else
                      printf '%s\\n' '{{"state":"OPEN","labels":[{{"name":"task:ready"}}],"title":"multi-remote claim","url":"https://example/issues/42"}}'
                    fi
                    ;;
                  "pr view")
                    if [ "${{CLAIM_TEST_PR_CONFLICT:-}}" = "1" ]; then
                      printf 'agent:%s,task:active,task:review\\n' "${{CLAIM_AGENT:-}}"
                    else
                      printf 'agent:%s,task:active\\n' "${{CLAIM_AGENT:-}}"
                    fi
                    ;;
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
                    if [ "${{CLAIM_TEST_FAIL_CREATE:-}}" = "1" ]; then
                      if [ -n "${{CLAIM_TEST_MUTATE_LOCAL_TO:-}}" ]; then
                        git -C "$CLAIM_TEST_CLONE" update-ref refs/heads/{CLAIM_BRANCH} "$CLAIM_TEST_MUTATE_LOCAL_TO"
                      fi
                      if [ "${{CLAIM_TEST_DIRTY_WORKTREE:-}}" = "1" ]; then
                        printf 'concurrent untracked work\\n' > "$CLAIM_TEST_WORKTREE_BASH/concurrent-untracked"
                      fi
                      echo 'fixture pr creation failure' >&2
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
            AI_TEAM_TEST_ORIGIN_REPO="example/canonical",
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

    def run_claim(
        self,
        *,
        worktree: Path,
        resume_branch: str | None = None,
        fail_create: bool = False,
        mutate_local_to: str | None = None,
        dirty_worktree: bool = False,
        leave_ready: bool = False,
        pr_conflict: bool = False,
        halt: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = self.git_env()
        env["CLAIM_TEST_GH_LOG"] = bash_path(self.gh_log)
        env["CLAIM_AGENT"] = "composer"
        env["CLAIM_WORKTREE"] = str(worktree)
        env["CLAIM_TEST_REAL_GIT"] = bash_path(Path(shutil.which("git") or "git"))
        env["CLAIM_TEST_BARE"] = bash_path(self.bare)
        env["CLAIM_TEST_CLONE"] = bash_path(self.clone)
        env["CLAIM_TEST_WORKTREE_BASH"] = bash_path(worktree)
        if resume_branch:
            env["CLAIM_BRANCH"] = resume_branch
        if fail_create:
            env["CLAIM_TEST_FAIL_CREATE"] = "1"
        if mutate_local_to:
            env["CLAIM_TEST_MUTATE_LOCAL_TO"] = mutate_local_to
        if dirty_worktree:
            env["CLAIM_TEST_DIRTY_WORKTREE"] = "1"
        if leave_ready:
            env["CLAIM_TEST_LEAVE_READY"] = "1"
        if pr_conflict:
            env["CLAIM_TEST_PR_CONFLICT"] = "1"
        if halt:
            env["CLAIM_TEST_HALT"] = "1"
        return run_with_bash_path(
            ["bash", bash_path(SCRIPT), "42"],
            stub_directory=self.bin,
            cwd=self.clone,
            env=env,
            capture_output=True,
            text=True,
        )

    def remote_ref(self, branch: str) -> str | None:
        result = subprocess.run(
            ["git", "--git-dir", str(self.bare), "rev-parse", "--verify", f"refs/heads/{branch}"],
            env=self.git_env(),
            text=True,
            capture_output=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

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
        self.assertIn(f"worktree: {bash_path(worktree)}", result.stdout)
        self.assertIn("root checkout left on main", result.stdout)
        self.assertEqual(self.git_out(["rev-parse", "--abbrev-ref", "HEAD"]), "main")
        self.assertRegex(self.gh_log.read_text(encoding="utf-8"), r"pr create .*--head agent/composer-issue-42")
        self.assertRegex(
            self.gh_log.read_text(encoding="utf-8"),
            r"pr create .*--body-file",
        )

    def test_claim_refuses_an_active_global_halt_before_mutating(self):
        result = self.run_claim(worktree=Path(self.dir.name) / "halted-lane", halt=True)

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("refusing to claim", result.stderr)
        self.assertIsNone(self.remote_ref("agent/composer-issue-42"))

    def test_new_claim_refuses_a_default_branch_behind_origin(self):
        other = Path(self.dir.name) / "updater"
        self.git(["clone", "-q", str(self.bare), str(other)], cwd=Path(self.dir.name))
        self.git(["commit", "--allow-empty", "-qm", "advance default"], cwd=other)
        self.git(["push", "-q", "origin", "main"], cwd=other)

        result = self.run_claim(worktree=Path(self.dir.name) / "behind-lane")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("must exactly match origin/main", result.stderr)
        self.assertIsNone(self.remote_ref("agent/composer-issue-42"))

    def test_fresh_claim_recycles_a_clean_parked_worktree(self):
        # One worktree per agent, reused across lanes: a registered, clean
        # worktree parked on origin/main is switched to the new lane branch
        # instead of a second checkout being created beside it.
        worktree = Path(self.dir.name) / "wt-recycled"
        self.git(["fetch", "-q", "origin", "main"])
        self.git(["worktree", "add", "-q", "--detach", str(worktree), "origin/main"])
        result = self.run_claim(worktree=worktree)
        self.assert_claim_success(result)
        self.assertEqual(self.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree), CLAIM_BRANCH)
        self.assertEqual(
            self.git_out(["worktree", "list", "--porcelain"]).count("worktree "), 2,
            "recycling must not add a worktree",
        )

    def test_fresh_claim_refuses_a_dirty_parked_worktree(self):
        worktree = Path(self.dir.name) / "wt-dirty-parked"
        self.git(["fetch", "-q", "origin", "main"])
        self.git(["worktree", "add", "-q", "--detach", str(worktree), "origin/main"])
        (worktree / "leftover.txt").write_text("from a previous lane\n", encoding="utf-8")
        result = self.run_claim(worktree=worktree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted changes from a previous lane", result.stderr)
        self.assertIsNone(self.remote_ref(CLAIM_BRANCH), "a refused claim must push nothing")

    def test_fresh_claim_refuses_a_parked_worktree_with_unpushed_work(self):
        worktree = Path(self.dir.name) / "wt-unpushed-parked"
        self.git(["fetch", "-q", "origin", "main"])
        self.git(["worktree", "add", "-q", "--detach", str(worktree), "origin/main"])
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "never pushed"],
            cwd=worktree, check=True, env=self.git_env(),
        )
        result = self.run_claim(worktree=worktree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("on no remote branch", result.stderr)

    def test_fresh_claim_rollback_preserves_a_concurrently_changed_local_ref(self):
        parent = self.git_out(["rev-parse", "HEAD"])
        tree = self.git_out(["rev-parse", "HEAD^{tree}"])
        replacement = subprocess.run(
            ["git", "commit-tree", tree, "-p", parent],
            cwd=self.clone,
            env=self.git_env(),
            input="concurrent local claim work\n",
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        worktree = Path(self.dir.name) / "wt-local-ref-race"

        result = self.run_claim(
            worktree=worktree,
            fail_create=True,
            mutate_local_to=replacement,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("local claim branch changed", result.stderr)
        self.assertIsNotNone(self.remote_ref(CLAIM_BRANCH))
        self.assertEqual(
            self.git_out(["rev-parse", f"refs/heads/{CLAIM_BRANCH}"]), replacement
        )
        self.assertTrue(worktree.exists())

    def test_fresh_claim_rollback_preserves_a_dirty_worktree_and_refs(self):
        worktree = Path(self.dir.name) / "wt-dirty-rollback"

        result = self.run_claim(
            worktree=worktree,
            fail_create=True,
            dirty_worktree=True,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("changed during rollback; preserving it and its refs", result.stderr)
        self.assertTrue((worktree / "concurrent-untracked").is_file())
        local_tip = self.git_out(["rev-parse", f"refs/heads/{CLAIM_BRANCH}"])
        self.assertEqual(self.remote_ref(CLAIM_BRANCH), local_tip)

    def test_clean_fresh_claim_failure_rolls_back_only_its_exact_lane(self):
        worktree = Path(self.dir.name) / "wt-clean-rollback"

        result = self.run_claim(worktree=worktree, fail_create=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rolling back the orphan claim branch", result.stderr)
        self.assertIsNone(self.remote_ref(CLAIM_BRANCH))
        self.assertIsNone(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.clone),
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{CLAIM_BRANCH}",
                ],
                env=self.git_env(),
                text=True,
                capture_output=True,
            ).stdout.strip()
            or None
        )
        self.assertFalse(worktree.exists())

    def test_claim_body_requires_closes_keyword(self):
        """Stub rejects claim bodies without Closes #N — the mechanism under test."""
        worktree = Path(self.dir.name) / "wt-closes"
        result = self.run_claim(worktree=worktree)
        self.assert_claim_success(result)
        # The stub already failed the run if Closes #42 was missing; log proves
        # the body-file path was used rather than an inline --body that could drift.
        self.assertRegex(self.gh_log.read_text(encoding="utf-8"), r"pr create .*--body-file")

    def test_claim_refuses_success_when_issue_still_has_ready_and_active(self):
        worktree = Path(self.dir.name) / "wt-contradictory-labels"

        result = self.run_claim(worktree=worktree, leave_ready=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("task state must be exactly task:active", result.stderr)
        self.assertIn("task:active,task:ready", result.stderr)
        self.assertIsNotNone(self.remote_ref(CLAIM_BRANCH))
        self.assertTrue(worktree.exists())

    def test_claim_refuses_success_when_pr_has_conflicting_task_state(self):
        worktree = Path(self.dir.name) / "wt-conflicting-pr-labels"

        result = self.run_claim(worktree=worktree, pr_conflict=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PR #99 task state must be exactly task:active", result.stderr)
        self.assertIn("task:active,task:review", result.stderr)
        self.assertIsNotNone(self.remote_ref(CLAIM_BRANCH))
        self.assertTrue(worktree.exists())

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
