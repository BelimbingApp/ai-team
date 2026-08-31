import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


TEMPLATE = Path(__file__).parents[1] / "templates" / "activate.sh"
UPDATE_BRANCH = "ai-team/package-refresh"


class ActivationRefreshTest(unittest.TestCase):
    """#38: activation refreshes one isolated package lane before onboarding."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source_bare = self.root / "package.git"
        self.source = self.root / "package-source"
        self.origin = self.root / "adopter.git"
        self.checkout = self.root / "checkout"
        self.state = self.root / "gh-state"
        self.state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.gh_log = self.state / "gh.log"
        self.suite_marker = self.root / "suite-ran"
        self.orient_marker = self.root / "orient-ran"
        self.activation_tmp = self.root / "activation-tmp"
        self.activation_tmp.mkdir()

        self._init_source()
        self._init_adopter()
        self._write_gh_stub()

    def git_env(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS=os.devnull,
            GIT_AUTHOR_NAME="activation-test",
            GIT_AUTHOR_EMAIL="activation-test@example.invalid",
            GIT_COMMITTER_NAME="activation-test",
            GIT_COMMITTER_EMAIL="activation-test@example.invalid",
        )
        return environment

    def git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.checkout,
            env=self.git_env(),
            text=True,
            capture_output=capture_output,
            check=True,
        )

    def git_output(self, *arguments: str, cwd: Path | None = None) -> str:
        return self.git(*arguments, cwd=cwd, capture_output=True).stdout.strip()

    def _init_source(self) -> None:
        self.git("init", "-q", "--bare", "-b", "main", str(self.source_bare), cwd=self.root)
        self.git("init", "-q", "-b", "main", str(self.source), cwd=self.root)

        # This is the legacy full-repository ref from which an existing adopter
        # may have mounted. package-mount is deliberately a separate root tree,
        # like the history emitted by `git subtree split --prefix=package`.
        (self.source / "README.md").write_text("legacy package repository\n", encoding="utf-8")
        legacy_scripts = self.source / "scripts"
        legacy_scripts.mkdir()
        (legacy_scripts / "orient.sh").write_text(
            "#!/usr/bin/env bash\nprintf 'legacy orient\\n'\n", encoding="utf-8"
        )
        (legacy_scripts / "orient.sh").chmod(0o755)
        legacy_ci = self.source / ".github" / "workflows"
        legacy_ci.mkdir(parents=True)
        (legacy_ci / "package-only.yml").write_text("name: should disappear\n", encoding="utf-8")
        self.git("add", "-A", cwd=self.source)
        self.git("commit", "-qm", "legacy full repository", cwd=self.source)
        self.git("remote", "add", "origin", str(self.source_bare), cwd=self.source)
        self.git("push", "-q", "-u", "origin", "main", cwd=self.source)

        self.git("switch", "--orphan", "package-mount", cwd=self.source)
        package_scripts = self.source / "scripts"
        package_scripts.mkdir()
        (package_scripts / "orient.sh").write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                printf 'oriented\\n'
                printf 'yes\\n' > "$ACTIVATION_TEST_ORIENT_MARKER"
                """
            ),
            encoding="utf-8",
        )
        (package_scripts / "orient.sh").chmod(0o755)
        (package_scripts / "test_smoke.py").write_text(
            textwrap.dedent(
                """\
                import os
                import unittest
                from pathlib import Path

                class MountedSuiteTest(unittest.TestCase):
                    def test_mounted_suite_runs(self):
                        with Path(os.environ["ACTIVATION_TEST_SUITE_MARKER"]).open(
                            "a", encoding="utf-8"
                        ) as marker:
                            marker.write("one updater\\n")

                if __name__ == "__main__":
                    unittest.main()
                """
            ),
            encoding="utf-8",
        )
        (self.source / "README.md").write_text("standalone mounted package\n", encoding="utf-8")
        self.git("add", "-A", cwd=self.source)
        self.git("commit", "-qm", "publish standalone package", cwd=self.source)
        self.git("push", "-q", "-u", "origin", "package-mount", cwd=self.source)
        self.package_sha = self.git_output("rev-parse", "HEAD", cwd=self.source)
        self.package_tree = self.git_output("rev-parse", "HEAD^{tree}", cwd=self.source)

    def _init_adopter(self) -> None:
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.root)
        seed = self.root / "adopter-seed"
        self.git("init", "-q", "-b", "main", str(seed), cwd=self.root)
        (seed / "README.md").write_text("adopter\n", encoding="utf-8")
        dot_ai_team = seed / ".ai-team"
        dot_ai_team.mkdir()
        activate = dot_ai_team / "activate.sh"
        activate.write_bytes(TEMPLATE.read_bytes())
        activate.chmod(0o755)
        (dot_ai_team / "package-refresh.conf").write_text(
            f"source={bash_path(self.source_bare)}\nref=package-mount\n",
            encoding="utf-8",
        )
        self.git("add", "-A", cwd=seed)
        self.git("commit", "-qm", "opt into activation refresh", cwd=seed)
        self.git("remote", "add", "origin", str(self.origin), cwd=seed)
        self.git("push", "-q", "-u", "origin", "main", cwd=seed)
        self.git("clone", "-q", str(self.origin), str(self.checkout), cwd=self.root)
        self.activate = self.checkout / ".ai-team" / "activate.sh"

    def _write_gh_stub(self) -> None:
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\\n' "$*" >> "$ACTIVATION_TEST_GH_LOG"
                state="$ACTIVATION_TEST_GH_STATE"
                case "$1 $2" in
                  "repo view")
                    printf 'example/adopter\\tmain\\n'
                    ;;
                  "issue list")
                    if [ -f "$state/active" ]; then
                      printf '#77\\n'
                    fi
                    ;;
                  "pr list")
                    has_head=false
                    for argument in "$@"; do
                      if [ "$argument" = "--head" ]; then
                        has_head=true
                      fi
                    done
                    if [ -n "${ACTIVATION_TEST_RACE_BARRIER:-}" ] && [ "$has_head" = true ]; then
                      : > "$ACTIVATION_TEST_RACE_BARRIER/$ACTIVATION_TEST_RACE_RUNNER"
                      while [ "$(find "$ACTIVATION_TEST_RACE_BARRIER" -type f | wc -l)" -lt 2 ]; do
                        sleep 1
                      done
                    fi
                    if [ "$has_head" = false ]; then
                      if [ -f "$state/active-pr" ]; then
                        printf 'PR #88\\n'
                      fi
                    elif [ -f "$state/pr-number" ]; then
                      number=$(cat "$state/pr-number")
                      draft=$(cat "$state/pr-draft")
                      head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse refs/heads/ai-team/package-refresh)
                      printf '%s\\t%s\\t%s\\thttps://example.test/pull/%s\\tagent:package-bootstrap\\ttrue\\ttrue\\n' "$number" "$draft" "$head" "$number"
                    fi
                    ;;
                  "pr create")
                    set -o noclobber
                    if ! printf '51\\n' > "$state/pr-number" 2>/dev/null; then
                      echo 'duplicate refresh PR' >&2
                      exit 1
                    fi
                    set +o noclobber
                    printf 'created\\n' >> "$state/pr-create-success"
                    printf 'true\\n' > "$state/pr-draft"
                    if [ -f "$state/activate-late-lane" ]; then
                      : > "$state/active-pr"
                    fi
                    previous=
                    for argument in "$@"; do
                      if [ "$previous" = "--body-file" ]; then
                        cp "$argument" "$state/pr-body"
                      fi
                      previous=$argument
                    done
                    printf 'https://example.test/pull/51\\n'
                    ;;
                  "pr view")
                    if [ ! -f "$state/pr-number" ]; then
                      exit 1
                    fi
                    if printf '%s\\n' "$*" | grep -q -- 'headRefOid'; then
                      head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse refs/heads/ai-team/package-refresh)
                      printf '%s\\tfalse\\tagent:package-bootstrap\\tfalse\\ttrue\\n' "$head"
                    else
                      printf '51\\thttps://example.test/pull/51\\n'
                    fi
                    ;;
                  "pr edit")
                    previous=
                    for argument in "$@"; do
                      if [ "$previous" = "--body-file" ]; then
                        cp "$argument" "$state/pr-body"
                      fi
                      previous=$argument
                    done
                    ;;
                  "pr ready")
                    if printf '%s\\n' "$*" | grep -q -- '--undo'; then
                      printf 'true\\n' > "$state/pr-draft"
                      rm -f "$state/ready"
                    else
                      printf 'false\\n' > "$state/pr-draft"
                      : > "$state/ready"
                    fi
                    ;;
                  "label create")
                    ;;
                  *)
                    echo "unexpected gh call: $*" >&2
                    exit 1
                    ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def run_activation(
        self,
        *,
        checkout: Path | None = None,
        race_barrier: Path | None = None,
        race_runner: str | None = None,
        fixed_commit_time: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        checkout = checkout or self.checkout
        environment = self.git_env()
        environment.update(
            ACTIVATION_TEST_GH_LOG=bash_path(self.gh_log),
            ACTIVATION_TEST_GH_STATE=bash_path(self.state),
            ACTIVATION_TEST_ORIGIN=bash_path(self.origin),
            ACTIVATION_TEST_SUITE_MARKER=str(self.suite_marker),
            ACTIVATION_TEST_ORIENT_MARKER=str(self.orient_marker),
            TMPDIR=bash_path(self.activation_tmp),
        )
        if race_barrier is not None:
            environment["ACTIVATION_TEST_RACE_BARRIER"] = bash_path(race_barrier)
            environment["ACTIVATION_TEST_RACE_RUNNER"] = race_runner or "runner"
        if fixed_commit_time:
            environment["GIT_AUTHOR_DATE"] = "2001-02-03T04:05:06+0000"
            environment["GIT_COMMITTER_DATE"] = "2001-02-03T04:05:06+0000"
        return run_with_bash_path(
            ["bash", str(checkout / ".ai-team" / "activate.sh")],
            stub_directory=self.bin,
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            timeout=180,
        )

    def remote_update_sha(self) -> str | None:
        result = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", "--verify", f"refs/heads/{UPDATE_BRANCH}"],
            env=self.git_env(),
            text=True,
            capture_output=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def origin_output(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(self.origin), *arguments],
            env=self.git_env(),
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def assert_caller_unchanged(
        self, original_head: str, *, checkout: Path | None = None
    ) -> None:
        checkout = checkout or self.checkout
        self.assertEqual(self.git_output("rev-parse", "HEAD", cwd=checkout), original_head)
        self.assertEqual(
            self.git_output("symbolic-ref", "--short", "HEAD", cwd=checkout), "main"
        )
        self.assertEqual(self.git_output("status", "--porcelain", cwd=checkout), "")
        self.assertEqual(list(self.activation_tmp.iterdir()), [])

    def test_concurrent_initial_refresh_is_idempotent_and_onboards_only_after_merge(self):
        original_head = self.git_output("rev-parse", "HEAD")
        second_checkout = self.root / "checkout-two"
        self.git("clone", "-q", str(self.origin), str(second_checkout), cwd=self.root)
        second_head = self.git_output("rev-parse", "HEAD", cwd=second_checkout)
        race_barrier = self.root / "race-barrier"
        race_barrier.mkdir()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self.run_activation,
                    checkout=checkout,
                    race_barrier=race_barrier,
                    race_runner=f"runner-{index}",
                    fixed_commit_time=True,
                )
                for index, checkout in enumerate((self.checkout, second_checkout), start=1)
            ]
            first, raced = [future.result() for future in futures]

        self.assertEqual(first.returncode, 3, first.stdout + first.stderr)
        self.assertEqual(raced.returncode, 3, raced.stdout + raced.stderr)
        combined_error = first.stderr + raced.stderr
        self.assertIn(self.package_sha, combined_error)
        self.assertRegex(
            combined_error,
            r"another updater changed|is claimed for package revision",
        )
        self.assertIn("onboarding is paused", first.stderr)
        self.assertTrue(self.suite_marker.is_file())
        self.assertEqual(
            self.suite_marker.read_text(encoding="utf-8").splitlines(),
            ["one updater"],
        )
        self.assertTrue((self.state / "ready").is_file())
        self.assertFalse(self.orient_marker.exists())
        self.assert_caller_unchanged(original_head)
        self.assertEqual(self.git_output("rev-parse", "HEAD", cwd=second_checkout), second_head)
        self.assertEqual(self.git_output("status", "--porcelain", cwd=second_checkout), "")
        update_sha = self.remote_update_sha()
        self.assertIsNotNone(update_sha)
        self.assertEqual(
            self.origin_output("rev-parse", f"{update_sha}:docs/ai-team"),
            self.package_tree,
        )
        metadata = self.origin_output("log", "-1", "--format=%B", update_sha)
        self.assertIn(f"AI-Team-Package-Ref: package-mount", metadata)
        self.assertIn(f"AI-Team-Package-Revision: {self.package_sha}", metadata)
        body = (self.state / "pr-body").read_text(encoding="utf-8")
        self.assertIn(self.package_sha, body)
        self.assertIn("full mechanism suite passed", body)
        self.assertEqual(
            (self.state / "pr-create-success").read_text(encoding="utf-8").splitlines(),
            ["created"],
        )

        second = self.run_activation()

        self.assertEqual(second.returncode, 3, second.stdout + second.stderr)
        self.assertIn("PR #51 is ready", second.stderr)
        self.assertEqual(self.remote_update_sha(), update_sha)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertEqual(log.count("pr create"), 1)

        subprocess.run(
            ["git", "--git-dir", str(self.origin), "update-ref", "refs/heads/main", update_sha],
            env=self.git_env(),
            check=True,
        )
        self.git("pull", "-q", "--ff-only", "origin", "main")
        mounted_orient = self.checkout / "docs" / "ai-team" / "scripts" / "orient.sh"
        mounted_orient.write_text(
            "#!/usr/bin/env bash\nprintf 'unverified local orient ran\\n'\n",
            encoding="utf-8",
        )
        dirty_mount = self.run_activation()

        self.assertEqual(dirty_mount.returncode, 1, dirty_mount.stdout + dirty_mount.stderr)
        self.assertIn("mounted package has unstaged changes", dirty_mount.stderr)
        self.assertFalse(self.orient_marker.exists())
        self.assertEqual(
            self.suite_marker.read_text(encoding="utf-8").splitlines(),
            ["one updater"],
        )
        self.git("restore", "docs/ai-team/scripts/orient.sh")
        after_merge = self.run_activation()

        self.assertEqual(after_merge.returncode, 0, after_merge.stdout + after_merge.stderr)
        self.assertIn("package is current", after_merge.stdout)
        self.assertTrue(self.orient_marker.is_file())
        self.assertEqual(
            self.suite_marker.read_text(encoding="utf-8").splitlines(),
            ["one updater", "one updater"],
        )

    def test_dirty_checkout_refuses_without_creating_remote_state(self):
        original_head = self.git_output("rev-parse", "HEAD")
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{UPDATE_BRANCH}")
        unknown_branch = self.remote_update_sha()

        unknown = self.run_activation()

        self.assertEqual(unknown.returncode, 1, unknown.stdout + unknown.stderr)
        self.assertIn("not an activation-owned branch", unknown.stderr)
        self.assertEqual(self.remote_update_sha(), unknown_branch)
        self.assert_caller_unchanged(original_head)
        self.git("push", "-q", "origin", "--delete", UPDATE_BRANCH)

        reject_hook = self.origin / "hooks" / "pre-receive"
        reject_hook.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                while read -r old new ref; do
                  if [ "$ref" = "refs/heads/ai-team/package-refresh" ]; then
                    echo "package refresh push denied by fixture" >&2
                    exit 1
                  fi
                done
                exit 0
                """
            ),
            encoding="utf-8",
        )
        reject_hook.chmod(reject_hook.stat().st_mode | stat.S_IXUSR)

        denied = self.run_activation()

        self.assertEqual(denied.returncode, 1, denied.stdout + denied.stderr)
        self.assertIn("cannot publish the package refresh claim", denied.stderr)
        self.assertNotIn("another updater", denied.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assert_caller_unchanged(original_head)
        reject_hook.unlink()

        policy = self.checkout / ".ai-team" / "package-refresh.conf"
        original_policy = policy.read_text(encoding="utf-8")
        policy.write_text(
            f"source={bash_path(self.source_bare)}\nref=missing-package-ref\n",
            encoding="utf-8",
        )

        unavailable = self.run_activation()

        self.assertEqual(unavailable.returncode, 1, unavailable.stdout + unavailable.stderr)
        self.assertIn("cannot resolve approved package ref", unavailable.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assertEqual(self.git_output("rev-parse", "HEAD"), original_head)
        self.assertNotEqual(self.git_output("status", "--porcelain"), "")
        policy.write_text(original_policy, encoding="utf-8")
        self.assertEqual(self.git_output("status", "--porcelain"), "")

        (self.checkout / "README.md").write_text("dirty adopter\n", encoding="utf-8")

        result = self.run_activation()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refuses a dirty checkout", result.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assertEqual(self.git_output("rev-parse", "HEAD"), original_head)
        self.assertFalse((self.state / "pr-number").exists())

    def test_issue_half_claim_and_pr_only_lane_each_refuse_the_update(self):
        original_head = self.git_output("rev-parse", "HEAD")
        (self.state / "active").write_text("yes\n", encoding="utf-8")

        issue_result = self.run_activation()

        self.assertEqual(issue_result.returncode, 1, issue_result.stdout + issue_result.stderr)
        self.assertIn("refuses active task lanes: #77", issue_result.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assert_caller_unchanged(original_head)

        (self.state / "active").unlink()
        (self.state / "active-pr").write_text("yes\n", encoding="utf-8")
        pr_result = self.run_activation()

        self.assertEqual(pr_result.returncode, 1, pr_result.stdout + pr_result.stderr)
        self.assertIn("refuses active task lanes: PR #88", pr_result.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assert_caller_unchanged(original_head)

        (self.state / "active-pr").unlink()
        (self.state / "activate-late-lane").write_text("yes\n", encoding="utf-8")
        late_lane = self.run_activation()

        self.assertEqual(late_lane.returncode, 1, late_lane.stdout + late_lane.stderr)
        self.assertIn("task lane appeared concurrently: PR #88", late_lane.stderr)
        self.assertIsNotNone(self.remote_update_sha())
        self.assertEqual(
            (self.state / "pr-draft").read_text(encoding="utf-8").strip(), "true"
        )
        self.assert_caller_unchanged(original_head)

    def test_failed_mounted_suite_leaves_caller_unchanged_and_pr_draft(self):
        package_test = self.source / "scripts" / "test_smoke.py"
        package_test.write_text(
            "import unittest\n\nclass Broken(unittest.TestCase):\n"
            "    def test_broken(self):\n        self.fail('expected fixture failure')\n",
            encoding="utf-8",
        )
        self.git("add", "scripts/test_smoke.py", cwd=self.source)
        self.git("commit", "-qm", "publish broken package", cwd=self.source)
        self.git("push", "-q", "origin", "package-mount", cwd=self.source)
        original_head = self.git_output("rev-parse", "HEAD")

        result = self.run_activation()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("mounted mechanism tests failed", result.stderr)
        self.assert_caller_unchanged(original_head)
        claim_sha = self.remote_update_sha()
        self.assertIsNotNone(claim_sha)
        missing_mount = subprocess.run(
            ["git", "--git-dir", str(self.origin), "cat-file", "-e", f"{claim_sha}:docs/ai-team"],
            env=self.git_env(),
            capture_output=True,
        )
        self.assertNotEqual(missing_mount.returncode, 0)
        self.assertEqual((self.state / "pr-draft").read_text(encoding="utf-8").strip(), "true")
        self.assertFalse((self.state / "ready").exists())
        failed_body = (self.state / "pr-body").read_text(encoding="utf-8")
        self.assertIn("Verification: pending", failed_body)
        self.assertNotIn("full mechanism suite passed", failed_body)

    def test_main_sourced_mount_migrates_exactly_to_package_mount(self):
        # Rebuild the adopter default with the supported legacy subtree mount.
        seed = self.root / "legacy-adopter"
        self.git("clone", "-q", str(self.origin), str(seed), cwd=self.root)
        self.git(
            "subtree",
            "add",
            "--prefix=docs/ai-team",
            str(self.source_bare),
            "main",
            "--squash",
            cwd=seed,
        )
        safe_legacy_sha = self.git_output("rev-parse", "HEAD", cwd=seed)
        (seed / "docs" / "ai-team" / "README.md").write_text(
            "adopter changed the legacy guide locally\n", encoding="utf-8"
        )
        self.git("add", "docs/ai-team/README.md", cwd=seed)
        self.git("commit", "-qm", "customize mounted legacy guide", cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)
        self.git("pull", "-q", "--ff-only", "origin", "main")
        conflicting_head = self.git_output("rev-parse", "HEAD")
        self.assertTrue((self.checkout / "docs" / "ai-team" / ".github").is_dir())

        conflict = self.run_activation()

        self.assertEqual(conflict.returncode, 1, conflict.stdout + conflict.stderr)
        self.assertIn("git subtree pull conflicted", conflict.stderr)
        self.assert_caller_unchanged(conflicting_head)
        self.assertEqual(
            (self.state / "pr-draft").read_text(encoding="utf-8").strip(), "true"
        )

        subprocess.run(
            ["git", "--git-dir", str(self.origin), "update-ref", "refs/heads/main", safe_legacy_sha],
            env=self.git_env(),
            check=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(self.origin), "update-ref", "-d", f"refs/heads/{UPDATE_BRANCH}"],
            env=self.git_env(),
            check=True,
        )
        for state_name in (
            "pr-number",
            "pr-draft",
            "pr-body",
            "ready",
            "pr-create-success",
        ):
            (self.state / state_name).unlink(missing_ok=True)
        safe_checkout = self.root / "legacy-safe-checkout"
        self.git("clone", "-q", str(self.origin), str(safe_checkout), cwd=self.root)
        original_head = self.git_output("rev-parse", "HEAD", cwd=safe_checkout)

        result = self.run_activation(checkout=safe_checkout)

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assert_caller_unchanged(original_head, checkout=safe_checkout)
        update_sha = self.remote_update_sha()
        self.assertEqual(
            self.origin_output("rev-parse", f"{update_sha}:docs/ai-team"),
            self.package_tree,
        )
        removed = subprocess.run(
            ["git", "--git-dir", str(self.origin), "cat-file", "-e", f"{update_sha}:docs/ai-team/.github"],
            env=self.git_env(),
            capture_output=True,
        )
        self.assertNotEqual(removed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
