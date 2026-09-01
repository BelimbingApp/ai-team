import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


TEMPLATE = Path(__file__).parents[1] / "templates" / "activate.sh"
CLAIM_SCRIPT = Path(__file__).parent / "claim.sh"
UPDATE_BRANCH = "ai-team/package-refresh"
MUTEX_BRANCH = "ai-team/activation-mutex"


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
        self.hook_marker = self.root / "metadata-hook-ran"
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
        (package_scripts / "claim.sh").write_text(
            "#!/usr/bin/env bash\nAI_TEAM_ACTIVATION_MUTEX_PROTOCOL=1\n",
            encoding="utf-8",
        )
        (package_scripts / "claim.sh").chmod(0o755)
        (package_scripts / "test_smoke.py").write_text(
            textwrap.dedent(
                """\
                import os
                import stat
                import subprocess
                import unittest
                from pathlib import Path

                class MountedSuiteTest(unittest.TestCase):
                    def test_mounted_suite_runs(self):
                        mutation = os.environ.get("ACTIVATION_TEST_SUITE_MUTATION")
                        if mutation in {"install-hook-only", "commit-outside"}:
                            common_dir = Path(
                                subprocess.run(
                                    ["git", "rev-parse", "--git-common-dir"],
                                    text=True,
                                    capture_output=True,
                                    check=True,
                                ).stdout.strip()
                            )
                            if not common_dir.is_absolute():
                                common_dir = (Path.cwd() / common_dir).resolve()
                            hook = common_dir / "hooks" / "post-commit"
                            hook.parent.mkdir(parents=True, exist_ok=True)
                            hook.write_text(
                                r'''#!/usr/bin/env bash
                                body=$(git log -1 --format=%B)
                                if printf '%s\\n' "$body" | grep -q '^chore(ai-team): refresh package' && ! printf '%s\\n' "$body" | grep -q '^AI-Team-Activation-Claim:'; then
                                  printf 'metadata hook ran\\n' > "$ACTIVATION_TEST_HOOK_MARKER"
                                fi
                                ''',
                                encoding="utf-8",
                            )
                            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
                        if mutation == "commit-outside":
                            sentinel = Path.cwd() / "ADOPTER_SUITE_SENTINEL"
                            sentinel.write_text("must never publish\\n", encoding="utf-8")
                            subprocess.run(["git", "add", sentinel.name], check=True)
                            subprocess.run(
                                ["git", "commit", "-qm", "suite mutated adopter root"],
                                check=True,
                            )
                        elif mutation == "rewrite-current":
                            orient = Path.cwd() / "docs" / "ai-team" / "scripts" / "orient.sh"
                            orient.write_text(
                                r'''#!/usr/bin/env bash
                                printf 'mutated orient ran\\n' > "$ACTIVATION_TEST_ORIENT_MARKER"
                                ''',
                                encoding="utf-8",
                            )
                            (Path.cwd() / "docs" / "ai-team" / "UNTRACKED_EXECUTION_TARGET").write_text(
                                "must never execute\\n", encoding="utf-8"
                            )
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
                    if printf '%s\\n' "$*" | grep -q -- 'defaultBranchRef'; then
                      printf 'example/adopter\\tmain\\n'
                    else
                      printf 'example/adopter\\n'
                    fi
                    ;;
                  "issue list")
                    if [ -f "$state/active" ]; then
                      printf '#77\\n'
                    fi
                    if [ -f "$state/blocked-only" ] && printf '%s\\n' "$*" | grep -q -- 'task:blocked'; then
                      printf '#66\\n'
                    fi
                    ;;
                  "issue view")
                    if printf '%s\\n' "$*" | grep -q -- '--jq'; then
                      printf 'agent:race-claimant,task:active\\n'
                    else
                      printf '{"state":"OPEN","labels":[{"name":"task:ready"}],"title":"Race claim","url":"https://example.test/issues/77"}\\n'
                    fi
                    ;;
                  "issue edit")
                    : > "$state/claim-issue-visible"
                    ;;
                  "pr list")
                    has_head=false
                    merged=false
                    for argument in "$@"; do
                      if [ "$argument" = "--head" ]; then
                        has_head=true
                      fi
                      if [ "$argument" = "merged" ]; then
                        merged=true
                      fi
                    done
                    if printf '%s\\n' "$*" | grep -q -- 'number,title,body,headRefName,labels,url'; then
                      printf '[]\\n'
                    elif [ "$merged" = true ]; then
                      if [ -f "$state/pr-merged" ]; then
                        if [ -f "$state/older-merged" ]; then
                          if printf '%s\\n' "$*" | grep -q -- 'Resolved revision'; then
                            printf '49\\t0000000000000000000000000000000000000000\\t0000000000000000000000000000000000000000\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\ttask:review\\ttrue\\ttrue\\t- Source: `%s`\\t- Ref: `package-mount`\\t- Resolved revision: `0000000000000000000000000000000000000000`\\n' "$ACTIVATION_TEST_SOURCE"
                          else
                            printf '49\\t0000000000000000000000000000000000000000\\t0000000000000000000000000000000000000000\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\ttrue\\ttrue\\n'
                          fi
                        fi
                        number=$(cat "$state/pr-number")
                        head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse --verify refs/heads/ai-team/package-refresh 2>/dev/null || cat "$state/pr-head")
                        merge=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse refs/heads/main)
                        revision=$(cat "$state/pr-package-sha")
                        if [ -f "$state/wrong-base-merged" ]; then
                          if printf '%s\\n' "$*" | grep -q -- 'Resolved revision'; then
                            printf '47\\t%s\\t%s\\texample/adopter\\tfalse\\trelease\\tai-team/package-refresh\\tagent:package-bootstrap\\ttask:review\\ttrue\\ttrue\\t- Source: `%s`\\t- Ref: `package-mount`\\t- Resolved revision: `%s`\\n' \\
                              "$head" "$merge" "$ACTIVATION_TEST_SOURCE" "$revision"
                          else
                            printf '47\\t%s\\t%s\\texample/adopter\\tfalse\\trelease\\tai-team/package-refresh\\tagent:package-bootstrap\\ttrue\\ttrue\\n' "$head" "$merge"
                          fi
                        fi
                        if [ -f "$state/valid-older-merged" ] && printf '%s\\n' "$*" | grep -q -- 'Resolved revision'; then
                          older_head=$(cat "$state/older-valid-head")
                          older_merge=$(cat "$state/older-valid-merge")
                          older_revision=$(cat "$state/older-valid-revision")
                          older_tasks=task:review
                          [ ! -f "$state/pr-terminal-48" ] || older_tasks=task:done
                          printf '48\\t%s\\t%s\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\t%s\\ttrue\\ttrue\\t- Source: `%s`\\t- Ref: `package-mount`\\t- Resolved revision: `%s`\\n' \\
                            "$older_head" "$older_merge" "$older_tasks" "$ACTIVATION_TEST_SOURCE" "$older_revision"
                        fi
                        if printf '%s\\n' "$*" | grep -q -- 'Resolved revision'; then
                          tasks=task:review
                          [ ! -f "$state/pr-done-plus-review" ] || tasks=task:done,task:review
                          [ ! -f "$state/pr-terminal-51" ] || tasks=task:done
                          printf '%s\\t%s\\t%s\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\t%s\\ttrue\\ttrue\\t- Source: `%s`\\t- Ref: `package-mount`\\t- Resolved revision: `%s`\\n' \\
                            "$number" "$head" "$merge" "$tasks" "$ACTIVATION_TEST_SOURCE" "$revision"
                        else
                          printf '%s\\t%s\\t%s\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\ttrue\\ttrue\\n' "$number" "$head" "$merge"
                        fi
                      fi
                    elif [ "$has_head" = false ]; then
                      if [ -f "$state/pr-number" ] && [ ! -f "$state/pr-merged" ] && [ ! -f "$state/pr-closed" ]; then
                        number=$(cat "$state/pr-number")
                        head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse refs/heads/ai-team/package-refresh)
                        printf '%s\\t%s\\tai-team/package-refresh\\tmain\\texample/adopter\\tfalse\\tagent:package-bootstrap\\ttrue\\ttrue\\n' "$number" "$head"
                      fi
                      if [ -f "$state/active-pr" ]; then
                        printf '88\\t0000000000000000000000000000000000000000\\tagent/other-issue-88\\tmain\\texample/adopter\\tfalse\\tagent:other\\tfalse\\tfalse\\n'
                      fi
                      if [ -f "$state/claim-pr-visible" ]; then
                        printf '52\\t0000000000000000000000000000000000000000\\tagent/race-claimant-issue-77\\tmain\\texample/adopter\\tfalse\\tagent:race-claimant\\tfalse\\tfalse\\n'
                      fi
                      if [ -f "$state/fork-refresh-pr" ]; then
                        printf '89\\t0000000000000000000000000000000000000000\\tai-team/package-refresh\\tmain\\tfork-owner/adopter\\ttrue\\tagent:package-bootstrap\\ttrue\\ttrue\\n'
                      fi
                      if [ -f "$state/other-base-refresh-pr" ]; then
                        other_head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse --verify refs/heads/ai-team/package-refresh 2>/dev/null || printf '0000000000000000000000000000000000000000')
                        printf '90\\t%s\\tai-team/package-refresh\\trelease\\texample/adopter\\tfalse\\tagent:package-bootstrap\\ttrue\\ttrue\\n' "$other_head"
                      fi
                    elif [ -f "$state/pr-number" ] && [ ! -f "$state/pr-merged" ] && [ ! -f "$state/pr-closed" ]; then
                      number=$(cat "$state/pr-number")
                      draft=$(cat "$state/pr-draft")
                      head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse refs/heads/ai-team/package-refresh)
                      tasks=task:active
                      [ ! -f "$state/verified-edit" ] || tasks=task:review
                      if [ -f "$state/extra-blocked" ]; then
                        tasks=$(printf '%s\\ntask:blocked\\n' "$tasks" | sort | paste -sd, -)
                      fi
                      [ ! -f "$state/contradictory-ready" ] || tasks=task:active,task:review
                      if printf '%s\\n' "$*" | grep -q -- 'number,isDraft'; then
                        printf '%s\\t%s\\t%s\\thttps://example.test/pull/%s\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\t%s\\ttrue\\ttrue\\n' "$number" "$draft" "$head" "$number" "$tasks"
                      elif printf '%s\\n' "$*" | grep -q -- 'number,url'; then
                        printf '%s\\thttps://example.test/pull/%s\\t%s\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\ttrue\\ttrue\\n' "$number" "$number" "$head"
                      else
                        printf '%s\\t%s\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\n' "$number" "$head"
                      fi
                    elif [ "$has_head" = true ] && [ -f "$state/fork-refresh-pr" ]; then
                      if printf '%s\\n' "$*" | grep -q -- 'number,isDraft'; then
                        printf '89\\ttrue\\t0000000000000000000000000000000000000000\\thttps://example.test/pull/89\\tfork-owner/adopter\\ttrue\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\ttask:active\\ttrue\\ttrue\\n'
                      elif printf '%s\\n' "$*" | grep -q -- 'number,url'; then
                        printf '89\\thttps://example.test/pull/89\\t0000000000000000000000000000000000000000\\tfork-owner/adopter\\ttrue\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\ttrue\\ttrue\\n'
                      else
                        printf '89\\t0000000000000000000000000000000000000000\\tfork-owner/adopter\\ttrue\\tmain\\tai-team/package-refresh\\n'
                      fi
                    elif [ "$has_head" = true ] && [ -f "$state/other-base-refresh-pr" ]; then
                      head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse --verify refs/heads/ai-team/package-refresh 2>/dev/null || printf '0000000000000000000000000000000000000000')
                      if printf '%s\\n' "$*" | grep -q -- 'number,isDraft'; then
                        printf '90\\ttrue\\t%s\\thttps://example.test/pull/90\\texample/adopter\\tfalse\\trelease\\tai-team/package-refresh\\tagent:package-bootstrap\\ttask:active\\ttrue\\ttrue\\n' "$head"
                      elif printf '%s\\n' "$*" | grep -q -- 'number,url'; then
                        printf '90\\thttps://example.test/pull/90\\t%s\\texample/adopter\\tfalse\\trelease\\tai-team/package-refresh\\tagent:package-bootstrap\\ttrue\\ttrue\\n' "$head"
                      else
                        printf '90\\t%s\\texample/adopter\\tfalse\\trelease\\tai-team/package-refresh\\n' "$head"
                      fi
                    fi
                    ;;
                  "pr create")
                    claim_head=false
                    previous=
                    for argument in "$@"; do
                      if [ "$previous" = "--head" ] && [ "$argument" != "ai-team/package-refresh" ]; then
                        claim_head=true
                      fi
                      previous=$argument
                    done
                    if [ "$claim_head" = true ]; then
                      : > "$state/claim-pr-visible"
                      printf 'https://example.test/pull/52\\n'
                      exit 0
                    fi
                    if [ -f "$state/pr-merged" ] || [ -f "$state/pr-closed" ]; then
                      printf '52\\n' > "$state/pr-number"
                      rm -f "$state/pr-merged" "$state/pr-closed"
                    else
                      set -o noclobber
                      if ! printf '51\\n' > "$state/pr-number" 2>/dev/null; then
                        echo 'duplicate refresh PR' >&2
                        exit 1
                      fi
                      set +o noclobber
                    fi
                    number=$(cat "$state/pr-number")
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
                    printf 'https://example.test/pull/%s\\n' "$number"
                    ;;
                  "pr view")
                    if [ "${3:-}" = "52" ] && [ -f "$state/claim-pr-visible" ]; then
                      printf 'agent:race-claimant,task:active\\n'
                      exit 0
                    fi
                    if [ ! -f "$state/pr-number" ]; then
                      exit 1
                    fi
                    if printf '%s\\n' "$*" | grep -q -- '--json labels'; then
                      if [ -f "$state/pr-terminal-${3:-}" ]; then
                        printf 'task:done\\n'
                      else
                        printf 'task:review\\n'
                      fi
                    elif printf '%s\\n' "$*" | grep -q -- 'headRefOid'; then
                      head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse refs/heads/ai-team/package-refresh)
                      if printf '%s\\n' "$*" | grep -q -- 'headRepository'; then
                        draft=$(cat "$state/pr-draft")
                        tasks=task:active
                        [ ! -f "$state/verified-edit" ] || tasks=task:review
                        if [ -f "$state/extra-blocked" ] || [ -f "$state/final-blocked" ]; then
                          tasks=$(printf '%s\\ntask:blocked\\n' "$tasks" | sort | paste -sd, -)
                        fi
                        if { [ "$draft" = "false" ] && [ -f "$state/bad-final-labels" ]; } || \
                           [ -f "$state/contradictory-ready" ]; then
                          tasks=task:active,task:review
                        fi
                        if printf '%s\\n' "$*" | grep -q -- 'startswith("task:")'; then
                          printf '%s\\t%s\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\t%s\\ttrue\\ttrue\\n' "$head" "$draft" "$tasks"
                        else
                          printf '%s\\t%s\\texample/adopter\\tfalse\\tmain\\tai-team/package-refresh\\tagent:package-bootstrap\\ttrue\\ttrue\\n' "$head" "$draft"
                        fi
                      else
                        draft=$(cat "$state/pr-draft")
                        printf '%s\\t%s\\tagent:package-bootstrap\\n' "$head" "$draft"
                      fi
                    else
                      number=$(cat "$state/pr-number")
                      printf '%s\\thttps://example.test/pull/%s\\n' "$number" "$number"
                    fi
                    ;;
                  "pr edit")
                    if printf '%s\\n' "$*" | grep -q -- '--add-label task:done'; then
                      : > "$state/pr-terminal-${3:-unknown}"
                      if [ "${3:-}" = "51" ]; then
                        : > "$state/pr-terminal"
                      fi
                    fi
                    if printf '%s\\n' "$*" | grep -q -- '--add-label task:review'; then
                      : > "$state/verified-edit"
                    fi
                    if printf '%s\\n' "$*" | grep -q -- '--remove-label task:review'; then
                      rm -f "$state/verified-edit"
                    fi
                    if printf '%s\\n' "$*" | grep -q -- '--remove-label task:blocked'; then
                      rm -f "$state/extra-blocked" "$state/final-blocked"
                    fi
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
                      if [ -f "$state/fail-next-undo" ]; then
                        rm -f "$state/fail-next-undo"
                        echo 'fixture draft transition failure' >&2
                        exit 1
                      fi
                      printf 'true\\n' > "$state/pr-draft"
                      rm -f "$state/ready"
                    else
                      if [ -f "$state/fail-next-ready" ]; then
                        rm -f "$state/fail-next-ready"
                        echo 'fixture ready failure' >&2
                        exit 1
                      fi
                      printf 'false\\n' > "$state/pr-draft"
                      : > "$state/ready"
                      if [ -f "$state/inject-blocked-on-ready" ]; then
                        : > "$state/final-blocked"
                      fi
                      head=$(git --git-dir="$ACTIVATION_TEST_ORIGIN" rev-parse refs/heads/ai-team/package-refresh)
                      printf '%s\\n' "$head" > "$state/pr-head"
                      printf '%s\\n' "$ACTIVATION_TEST_PACKAGE_SHA" > "$state/pr-package-sha"
                      number=$(cat "$state/pr-number")
                      git --git-dir="$ACTIVATION_TEST_ORIGIN" update-ref "refs/pull/$number/head" "$head"
                    fi
                    ;;
                  "label create")
                    ;;
                  "label list")
                    printf '[{"name":"agent:race-claimant"}]\\n'
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

    def install_git_race_shim(self) -> None:
        git = self.bin / "git"
        git.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -eu
                if [ -n "${AI_TEAM_TEST_ORIGIN_REPO:-}" ] && \
                   [ "${1:-}" = "remote" ] && [ "${2:-}" = "get-url" ] && [ "${3:-}" = "origin" ]; then
                  printf 'https://github.com/%s.git\\n' "$AI_TEAM_TEST_ORIGIN_REPO"
                  exit 0
                fi
                arguments=("$@")
                command_index=0
                while [ "$command_index" -lt "${#arguments[@]}" ]; do
                  case "${arguments[$command_index]}" in
                    -c|-C|--git-dir|--work-tree|--namespace)
                      command_index=$((command_index + 2))
                      ;;
                    --git-dir=*|--work-tree=*|--namespace=*|--no-pager|--literal-pathspecs|--no-literal-pathspecs)
                      command_index=$((command_index + 1))
                      ;;
                    *)
                      break
                      ;;
                  esac
                done
                git_command="${arguments[$command_index]:-}"
                mutex_create=false
                mutex_source=''
                if [ "$git_command" = "push" ]; then
                  for argument in "$@"; do
                    case "$argument" in
                      [0-9a-fA-F]*:refs/heads/ai-team/activation-mutex)
                        mutex_create=true
                        mutex_source="${argument%%:*}"
                        ;;
                    esac
                  done
                fi
                mutex_ref_argument=false
                for argument in "$@"; do
                  [ "$argument" != "refs/heads/ai-team/activation-mutex" ] || \
                    mutex_ref_argument=true
                done
                if [ "$git_command" = "ls-remote" ] && \
                   [ "$mutex_ref_argument" = true ] && \
                   [ "${ACTIVATION_TEST_MUTEX_TRANSITION:-}" = "wait-clear" ] && \
                   [ -f "$ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE.transition-installed" ]; then
                  if [ -f "$ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE.transition-read" ]; then
                    "$ACTIVATION_TEST_REAL_GIT" --git-dir="$ACTIVATION_TEST_ORIGIN" \
                      update-ref -d refs/heads/ai-team/activation-mutex
                    exit 0
                  fi
                  : > "$ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE.transition-read"
                fi
                if [ "$mutex_create" = true ] && [ -n "${ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE:-}" ]; then
                  printf '%s\\n' "$mutex_source" >> "$ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE"
                  mutex_attempt=$(wc -l < "$ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE")
                  mutex_attempt=${mutex_attempt//[[:space:]]/}
                  if [ "$mutex_attempt" -le "${ACTIVATION_TEST_EMPTY_MUTEX_FAILURES:-0}" ]; then
                    if [ "$mutex_attempt" -eq 2 ] && \
                       [ -n "${ACTIVATION_TEST_MUTEX_TRANSITION:-}" ]; then
                      transition_sha="$mutex_source"
                      if [ "$ACTIVATION_TEST_MUTEX_TRANSITION" = "wait-clear" ] || \
                         [ "$ACTIVATION_TEST_MUTEX_TRANSITION" = "exact-recovery" ]; then
                        transition_sha="$ACTIVATION_TEST_MUTEX_TRANSITION_SHA"
                      fi
                      "$ACTIVATION_TEST_REAL_GIT" --git-dir="$ACTIVATION_TEST_ORIGIN" \
                        update-ref refs/heads/ai-team/activation-mutex "$transition_sha"
                      : > "$ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE.transition-installed"
                    fi
                    printf 'simulated empty mutex CAS failure %s\\n' "$mutex_attempt" >&2
                    exit 91
                  fi
                fi
                if [ "$mutex_create" = true ] && [ -n "${ACTIVATION_TEST_RACE_BARRIER:-}" ]; then
                  : > "$ACTIVATION_TEST_RACE_BARRIER/$ACTIVATION_TEST_RACE_RUNNER"
                  attempts=0
                  while [ "$(find "$ACTIVATION_TEST_RACE_BARRIER" -type f | wc -l)" -lt 2 ]; do
                    attempts=$((attempts + 1))
                    [ "$attempts" -lt 600 ] || exit 97
                    sleep 0.1
                  done
                fi
                if [ "$mutex_create" = true ]; then
                  if "$ACTIVATION_TEST_REAL_GIT" "$@"; then
                    status=0
                  else
                    status=$?
                  fi
                  if [ "$status" -eq 0 ] && [ -n "${ACTIVATION_TEST_MUTEX_WINNER_DELAY:-}" ]; then
                    sleep "$ACTIVATION_TEST_MUTEX_WINNER_DELAY"
                  fi
                  exit "$status"
                fi
                exec "$ACTIVATION_TEST_REAL_GIT" "$@"
                """
            ),
            encoding="utf-8",
        )
        git.chmod(git.stat().st_mode | stat.S_IXUSR)

    def run_activation(
        self,
        *,
        checkout: Path | None = None,
        race_barrier: Path | None = None,
        race_runner: str | None = None,
        fixed_commit_time: bool = False,
        exclusive_first_refresh: bool = False,
        recover_mutex_sha: str | None = None,
        recover_refresh_sha: str | None = None,
        mutex_wait_seconds: int | None = None,
        mutex_winner_delay: int | None = None,
        suite_mutation: str | None = None,
        git_dir: Path | None = None,
        mutex_empty_failures: int | None = None,
        mutex_attempts_file: Path | None = None,
        mutex_transition: str | None = None,
        mutex_transition_sha: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        checkout = checkout or self.checkout
        environment = self.git_env()
        environment.update(
            ACTIVATION_TEST_GH_LOG=bash_path(self.gh_log),
            ACTIVATION_TEST_GH_STATE=bash_path(self.state),
            ACTIVATION_TEST_ORIGIN=bash_path(self.origin),
            ACTIVATION_TEST_PACKAGE_SHA=self.package_sha,
            ACTIVATION_TEST_SOURCE=bash_path(self.source_bare),
            ACTIVATION_TEST_SUITE_MARKER=str(self.suite_marker),
            ACTIVATION_TEST_ORIENT_MARKER=str(self.orient_marker),
            ACTIVATION_TEST_HOOK_MARKER=str(self.hook_marker),
            TMPDIR=bash_path(self.activation_tmp),
            ACTIVATION_TEST_REAL_GIT=bash_path(Path(shutil.which("git") or "git")),
        )
        if race_barrier is not None:
            environment["ACTIVATION_TEST_RACE_BARRIER"] = bash_path(race_barrier)
            environment["ACTIVATION_TEST_RACE_RUNNER"] = race_runner or "runner"
        if fixed_commit_time:
            environment["GIT_AUTHOR_DATE"] = "2001-02-03T04:05:06+0000"
            environment["GIT_COMMITTER_DATE"] = "2001-02-03T04:05:06+0000"
        if exclusive_first_refresh:
            environment["AI_TEAM_EXCLUSIVE_FIRST_REFRESH"] = "1"
        if recover_mutex_sha is not None:
            environment["AI_TEAM_RECOVER_MUTEX_SHA"] = recover_mutex_sha
        if recover_refresh_sha is not None:
            environment["AI_TEAM_RECOVER_REFRESH_SHA"] = recover_refresh_sha
        if mutex_wait_seconds is not None:
            environment["AI_TEAM_MUTEX_WAIT_SECONDS"] = str(mutex_wait_seconds)
        if mutex_winner_delay is not None:
            environment["ACTIVATION_TEST_MUTEX_WINNER_DELAY"] = str(mutex_winner_delay)
        if suite_mutation is not None:
            environment["ACTIVATION_TEST_SUITE_MUTATION"] = suite_mutation
        if git_dir is not None:
            environment["GIT_DIR"] = str(git_dir)
        if mutex_empty_failures is not None:
            environment["ACTIVATION_TEST_EMPTY_MUTEX_FAILURES"] = str(
                mutex_empty_failures
            )
        if mutex_attempts_file is not None:
            environment["ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE"] = bash_path(
                mutex_attempts_file
            )
        if mutex_transition is not None:
            environment["ACTIVATION_TEST_MUTEX_TRANSITION"] = mutex_transition
        if mutex_transition_sha is not None:
            environment["ACTIVATION_TEST_MUTEX_TRANSITION_SHA"] = mutex_transition_sha
        return run_with_bash_path(
            ["bash", bash_path(checkout / ".ai-team" / "activate.sh")],
            stub_directory=self.bin,
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            timeout=600,
        )

    def run_claim(
        self,
        *,
        checkout: Path | None = None,
        race_barrier: Path | None = None,
        race_runner: str | None = None,
        mutex_empty_failures: int | None = None,
        mutex_attempts_file: Path | None = None,
        mutex_transition: str | None = None,
        mutex_transition_sha: str | None = None,
        recover_mutex_sha: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        checkout = checkout or self.checkout
        environment = self.git_env()
        environment.update(
            ACTIVATION_TEST_GH_LOG=bash_path(self.gh_log),
            ACTIVATION_TEST_GH_STATE=bash_path(self.state),
            ACTIVATION_TEST_ORIGIN=bash_path(self.origin),
            CLAIM_AGENT="race-claimant",
            CLAIM_WORKTREE=bash_path(self.root / "claim-lane"),
            ACTIVATION_TEST_REAL_GIT=bash_path(Path(shutil.which("git") or "git")),
            AI_TEAM_TEST_ORIGIN_REPO="example/adopter",
            GIT_AUTHOR_DATE="2001-02-03T04:05:06+0000",
            GIT_COMMITTER_DATE="2001-02-03T04:05:06+0000",
        )
        if race_barrier is not None:
            environment["ACTIVATION_TEST_RACE_BARRIER"] = bash_path(race_barrier)
            environment["ACTIVATION_TEST_RACE_RUNNER"] = race_runner or "claim"
        if mutex_empty_failures is not None:
            environment["ACTIVATION_TEST_EMPTY_MUTEX_FAILURES"] = str(
                mutex_empty_failures
            )
        if mutex_attempts_file is not None:
            environment["ACTIVATION_TEST_MUTEX_ATTEMPTS_FILE"] = bash_path(
                mutex_attempts_file
            )
        if mutex_transition is not None:
            environment["ACTIVATION_TEST_MUTEX_TRANSITION"] = mutex_transition
        if mutex_transition_sha is not None:
            environment["ACTIVATION_TEST_MUTEX_TRANSITION_SHA"] = mutex_transition_sha
        if recover_mutex_sha is not None:
            environment["AI_TEAM_RECOVER_MUTEX_SHA"] = recover_mutex_sha
        return run_with_bash_path(
            ["bash", bash_path(CLAIM_SCRIPT), "77"],
            stub_directory=self.bin,
            cwd=checkout,
            env=environment,
            text=True,
            capture_output=True,
            timeout=600,
        )

    def remote_update_sha(self) -> str | None:
        result = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", "--verify", f"refs/heads/{UPDATE_BRANCH}"],
            env=self.git_env(),
            text=True,
            capture_output=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def remote_mutex_sha(self) -> str | None:
        result = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", "--verify", f"refs/heads/{MUTEX_BRANCH}"],
            env=self.git_env(),
            text=True,
            capture_output=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def mutex_attempt_shas(self, attempts_file: Path) -> list[str]:
        return attempts_file.read_text(encoding="utf-8").splitlines()

    def assert_fresh_mutex_attempts(
        self, attempts_file: Path, expected_count: int
    ) -> list[str]:
        attempts = self.mutex_attempt_shas(attempts_file)
        self.assertEqual(len(attempts), expected_count, attempts)
        self.assertEqual(len(set(attempts)), expected_count, attempts)
        nonces = []
        for attempt in attempts:
            message = self.git_output("show", "-s", "--format=%B", attempt)
            nonce_lines = [
                line.removeprefix("AI-Team-Activation-Mutex-Nonce: ")
                for line in message.splitlines()
                if line.startswith("AI-Team-Activation-Mutex-Nonce: ")
            ]
            self.assertEqual(len(nonce_lines), 1, message)
            self.assertRegex(nonce_lines[0], r"^[0-9a-fA-F]{32}$")
            nonces.append(nonce_lines[0].lower())
        self.assertEqual(len(set(nonces)), expected_count, nonces)
        return attempts

    def create_stale_generated_mutex(self) -> str:
        parent = self.git_output("rev-parse", "HEAD")
        tree = self.git_output("rev-parse", "HEAD^{tree}")
        message = (
            "AI Team activation/claim mutex\n\n"
            "AI-Team-Activation-Mutex: true\n"
            f"AI-Team-Activation-Mutex-Base: {parent}\n"
            f"AI-Team-Activation-Mutex-Owner: package-refresh:{self.package_sha}\n"
            "AI-Team-Activation-Mutex-Nonce: 0123456789abcdef0123456789abcdef\n"
        )
        created = subprocess.run(
            ["git", "commit-tree", tree, "-p", parent],
            cwd=self.checkout,
            env=self.git_env(),
            input=message,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.git("push", "-q", "origin", f"{created}:refs/heads/{MUTEX_BRANCH}")
        return created

    def create_orphan_refresh_claim(self) -> str:
        base = self.git_output("rev-parse", "HEAD")
        tree = self.git_output("rev-parse", "HEAD^{tree}")
        message = (
            "AI Team package refresh claim\n\n"
            "AI-Team-Activation-Managed: true\n"
            f"AI-Team-Activation-Base: {base}\n"
            f"AI-Team-Package-Source: {bash_path(self.source_bare)}\n"
            "AI-Team-Package-Ref: package-mount\n"
            f"AI-Team-Package-Revision: {self.package_sha}\n"
            "AI-Team-Activation-Claim: 0123456789abcdef0123456789abcdef\n"
        )
        created = subprocess.run(
            ["git", "commit-tree", tree, "-p", base],
            cwd=self.checkout,
            env=self.git_env(),
            input=message,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.git("push", "-q", "origin", f"{created}:refs/heads/{UPDATE_BRANCH}")
        return created

    def expose_refresh_pr(self, *, draft: bool = True) -> None:
        (self.state / "pr-number").write_text("51\n", encoding="utf-8")
        (self.state / "pr-draft").write_text(
            f"{'true' if draft else 'false'}\n", encoding="utf-8"
        )

    def create_forged_refresh_with_package_sentinel(self, marker: str = "") -> str:
        base = self.git_output("rev-parse", "HEAD")
        worktree = self.root / "forged-refresh-worktree"
        self.git("worktree", "add", "-q", "--detach", str(worktree), base)
        mounted = worktree / "docs" / "ai-team"
        mounted.mkdir(parents=True)
        (mounted / "ADOPTER_SENTINEL").write_text(
            "must never be overwritten\n", encoding="utf-8"
        )
        message = (
            "forged generated-looking refresh\n\n"
            "AI-Team-Activation-Managed: true\n"
            f"AI-Team-Activation-Base: {base}\n"
            f"AI-Team-Package-Source: {bash_path(self.source_bare)}\n"
            "AI-Team-Package-Ref: package-mount\n"
            f"AI-Team-Package-Revision: {self.package_sha}\n"
            f"{marker}"
        )
        self.git("add", "docs/ai-team/ADOPTER_SENTINEL", cwd=worktree)
        subprocess.run(
            ["git", "commit", "-q", "--file=-"],
            cwd=worktree,
            env=self.git_env(),
            input=message,
            text=True,
            check=True,
        )
        forged_sha = self.git_output("rev-parse", "HEAD", cwd=worktree)
        self.git(
            "push",
            "-q",
            "origin",
            f"{forged_sha}:refs/heads/{UPDATE_BRANCH}",
            cwd=worktree,
        )
        self.git("worktree", "remove", str(worktree))
        return forged_sha

    def advance_package_source(self, name: str = "NEXT") -> str:
        (self.source / name).write_text("newer approved package\n", encoding="utf-8")
        self.git("add", name, cwd=self.source)
        self.git("commit", "-qm", f"advance package with {name}", cwd=self.source)
        self.git("push", "-q", "origin", "package-mount", cwd=self.source)
        self.package_sha = self.git_output("rev-parse", "HEAD", cwd=self.source)
        self.package_tree = self.git_output("rev-parse", "HEAD^{tree}", cwd=self.source)
        return self.package_sha

    def append_trailer_to_refresh(self, trailer: str) -> str:
        current = self.remote_update_sha()
        self.assertIsNotNone(current)
        worktree = self.root / "refresh-trailer-worktree"
        self.git("worktree", "add", "-q", "--detach", str(worktree), current)
        message = self.origin_output("log", "-1", "--format=%B", current)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-q", "--file=-"],
            cwd=worktree,
            env=self.git_env(),
            input=f"{message.rstrip()}\n{trailer}\n",
            text=True,
            check=True,
        )
        new_head = self.git_output("rev-parse", "HEAD", cwd=worktree)
        self.git(
            "push",
            "-q",
            f"--force-with-lease=refs/heads/{UPDATE_BRANCH}:{current}",
            "origin",
            f"{new_head}:refs/heads/{UPDATE_BRANCH}",
            cwd=worktree,
        )
        self.git("worktree", "remove", str(worktree))
        return new_head

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
        self.install_git_race_shim()
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
                    mutex_wait_seconds=120,
                    exclusive_first_refresh=True,
                )
                for index, checkout in enumerate((self.checkout, second_checkout), start=1)
            ]
            first, raced = [future.result() for future in futures]

        self.assertEqual(first.returncode, 3, first.stdout + first.stderr)
        self.assertEqual(raced.returncode, 3, raced.stdout + raced.stderr)
        combined_error = first.stderr + raced.stderr
        self.assertIn(self.package_sha, combined_error)
        self.assertIn("the short mutex cleared", combined_error)
        self.assertIn("PR #51", combined_error)
        self.assertIn("onboarding is paused", combined_error)
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
        self.assertIsNone(self.remote_mutex_sha())

        second = self.run_activation(exclusive_first_refresh=True)

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
        (self.state / "pr-merged").write_text("yes\n", encoding="utf-8")
        mounted_orient = self.checkout / "docs" / "ai-team" / "scripts" / "orient.sh"
        mounted_orient.write_text(
            "#!/usr/bin/env bash\nprintf 'unverified local orient ran\\n'\n",
            encoding="utf-8",
        )
        dirty_mount = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(dirty_mount.returncode, 1, dirty_mount.stdout + dirty_mount.stderr)
        self.assertIn("mounted package has unstaged changes", dirty_mount.stderr)
        self.assertFalse(self.orient_marker.exists())
        self.assertEqual(
            self.suite_marker.read_text(encoding="utf-8").splitlines(),
            ["one updater"],
        )
        self.git("restore", "docs/ai-team/scripts/orient.sh")

        mounted_orient.write_text("#!/usr/bin/env bash\nprintf 'staged local orient ran\\n'\n", encoding="utf-8")
        self.git("add", "docs/ai-team/scripts/orient.sh")
        staged_mount = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(staged_mount.returncode, 1, staged_mount.stdout + staged_mount.stderr)
        self.assertIn("mounted package has staged changes", staged_mount.stderr)
        self.git("restore", "--staged", "docs/ai-team/scripts/orient.sh")
        self.git("restore", "docs/ai-team/scripts/orient.sh")

        untracked = self.checkout / "docs" / "ai-team" / "local-untracked"
        untracked.write_text("do not execute through this tree\n", encoding="utf-8")
        untracked_mount = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(
            untracked_mount.returncode, 1, untracked_mount.stdout + untracked_mount.stderr
        )
        self.assertIn("mounted package has untracked files", untracked_mount.stderr)
        untracked.unlink()

        (self.state / "older-merged").write_text("yes\n", encoding="utf-8")
        after_merge = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(after_merge.returncode, 0, after_merge.stdout + after_merge.stderr)
        self.assertIn("package is current", after_merge.stdout)
        self.assertIn("finalized merged package refresh PR #51", after_merge.stdout)
        self.assertTrue(self.orient_marker.is_file())
        self.assertIsNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        self.assertTrue((self.state / "pr-terminal").is_file())
        self.assertNotIn("pr edit 49 --repo", self.gh_log.read_text(encoding="utf-8"))
        temp_pr_ref = subprocess.run(
            ["git", "rev-parse", "--verify", "refs/ai-team/activation-pr-head"],
            cwd=self.checkout,
            env=self.git_env(),
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(temp_pr_ref.returncode, 0)
        self.assertEqual(
            self.suite_marker.read_text(encoding="utf-8").splitlines(),
            ["one updater", "one updater"],
        )

    def test_real_claim_and_activation_share_one_atomic_mutex(self):
        original_head = self.git_output("rev-parse", "HEAD")
        self.install_git_race_shim()
        race_barrier = self.root / "claim-activation-barrier"
        race_barrier.mkdir()

        with ThreadPoolExecutor(max_workers=2) as executor:
            activation_future = executor.submit(
                self.run_activation,
                race_barrier=race_barrier,
                race_runner="activation",
                fixed_commit_time=True,
                exclusive_first_refresh=True,
            )
            claim_future = executor.submit(
                self.run_claim,
                race_barrier=race_barrier,
                race_runner="claim",
            )
            activation = activation_future.result()
            claim = claim_future.result()

        combined = activation.stdout + activation.stderr + claim.stdout + claim.stderr
        self.assertIn(
            (activation.returncode, claim.returncode),
            ((3, 1), (1, 0)),
            combined,
        )
        refresh_won = self.remote_update_sha() is not None
        claim_branch = self.origin_output(
            "show-ref", "--verify", "--hash", "refs/heads/agent/race-claimant-issue-77"
        ) if claim.returncode == 0 else ""
        self.assertEqual(bool(refresh_won), not bool(claim_branch), combined)
        self.assertEqual((self.state / "pr-number").exists(), refresh_won)
        self.assertEqual((self.state / "claim-pr-visible").exists(), not refresh_won)
        self.assertIsNone(self.remote_mutex_sha())
        self.assertEqual(self.git_output("rev-parse", "HEAD"), original_head)
        self.assertEqual(self.git_output("status", "--porcelain"), "")
        self.assertEqual(sorted(path.name for path in race_barrier.iterdir()), ["activation", "claim"])

    def test_activation_retries_an_empty_mutex_cas_with_a_fresh_nonce(self):
        self.install_git_race_shim()
        attempts_file = self.root / "activation-mutex-attempts"

        result = self.run_activation(
            fixed_commit_time=True,
            exclusive_first_refresh=True,
            mutex_empty_failures=1,
            mutex_attempts_file=attempts_file,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn(
            "mutex CAS failed but the ref is now empty; retrying with a fresh nonce (1/3)",
            output,
        )
        self.assert_fresh_mutex_attempts(attempts_file, 2)
        self.assertIsNotNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        self.assertTrue((self.state / "ready").is_file())

    def test_activation_empty_mutex_cas_retry_budget_is_bounded(self):
        self.install_git_race_shim()
        attempts_file = self.root / "activation-mutex-denials"

        result = self.run_activation(
            fixed_commit_time=True,
            exclusive_first_refresh=True,
            mutex_empty_failures=4,
            mutex_attempts_file=attempts_file,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("check push permission or protection", output)
        self.assert_fresh_mutex_attempts(attempts_file, 4)
        self.assertIsNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        self.assertFalse((self.state / "pr-number").exists())

    def test_activation_waited_clear_starts_a_fresh_empty_cas_budget(self):
        self.install_git_race_shim()
        stale_mutex = self.create_stale_generated_mutex()
        self.git("push", "-q", "origin", "--delete", MUTEX_BRANCH)
        attempts_file = self.root / "activation-mutex-wait-reset"

        result = self.run_activation(
            fixed_commit_time=True,
            exclusive_first_refresh=True,
            mutex_empty_failures=5,
            mutex_attempts_file=attempts_file,
            mutex_transition="wait-clear",
            mutex_transition_sha=stale_mutex,
            mutex_wait_seconds=2,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("the short mutex cleared after 1 second(s)", output)
        self.assertEqual(output.count("fresh nonce (1/3)"), 2, output)
        self.assertEqual(output.count("fresh nonce (2/3)"), 1, output)
        self.assertEqual(output.count("fresh nonce (3/3)"), 1, output)
        self.assert_fresh_mutex_attempts(attempts_file, 6)
        self.assertIsNotNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())

    def test_activation_exact_recovery_starts_a_fresh_empty_cas_budget(self):
        self.install_git_race_shim()
        stale_mutex = self.create_stale_generated_mutex()
        self.git("push", "-q", "origin", "--delete", MUTEX_BRANCH)
        attempts_file = self.root / "activation-mutex-recovery-reset"

        result = self.run_activation(
            fixed_commit_time=True,
            exclusive_first_refresh=True,
            recover_mutex_sha=stale_mutex,
            mutex_empty_failures=5,
            mutex_attempts_file=attempts_file,
            mutex_transition="exact-recovery",
            mutex_transition_sha=stale_mutex,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn(
            f"recovered exact stale generated mutex {stale_mutex}", output
        )
        self.assertEqual(output.count("fresh nonce (1/3)"), 2, output)
        self.assertEqual(output.count("fresh nonce (2/3)"), 1, output)
        self.assertEqual(output.count("fresh nonce (3/3)"), 1, output)
        self.assert_fresh_mutex_attempts(attempts_file, 6)
        self.assertIsNotNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())

    def test_claim_retries_an_empty_mutex_cas_with_a_fresh_nonce(self):
        self.install_git_race_shim()
        attempts_file = self.root / "claim-mutex-attempts"

        result = self.run_claim(
            mutex_empty_failures=1,
            mutex_attempts_file=attempts_file,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn(
            "mutex CAS failed but the ref is now empty; retrying with a fresh nonce (1/3)",
            output,
        )
        self.assert_fresh_mutex_attempts(attempts_file, 2)
        self.assertTrue((self.state / "claim-pr-visible").is_file())
        self.assertIsNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        claimed = self.origin_output(
            "show-ref", "--verify", "--hash", "refs/heads/agent/race-claimant-issue-77"
        )
        self.assertRegex(claimed, r"^[0-9a-f]{40,64}$")

    def test_claim_empty_mutex_cas_retry_budget_is_bounded(self):
        self.install_git_race_shim()
        attempts_file = self.root / "claim-mutex-denials"

        result = self.run_claim(
            mutex_empty_failures=4,
            mutex_attempts_file=attempts_file,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("check push permission or protection", output)
        self.assert_fresh_mutex_attempts(attempts_file, 4)
        self.assertIsNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        self.assertFalse((self.state / "claim-pr-visible").exists())
        missing_claim = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.origin),
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/agent/race-claimant-issue-77",
            ],
            env=self.git_env(),
            check=False,
        )
        self.assertNotEqual(missing_claim.returncode, 0)

    def test_claim_exact_recovery_starts_a_fresh_empty_cas_budget(self):
        self.install_git_race_shim()
        stale_mutex = self.create_stale_generated_mutex()
        self.git("push", "-q", "origin", "--delete", MUTEX_BRANCH)
        attempts_file = self.root / "claim-mutex-recovery-reset"

        result = self.run_claim(
            mutex_empty_failures=5,
            mutex_attempts_file=attempts_file,
            mutex_transition="exact-recovery",
            mutex_transition_sha=stale_mutex,
            recover_mutex_sha=stale_mutex,
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn(
            f"recovered exact stale generated mutex {stale_mutex}", output
        )
        self.assertEqual(output.count("fresh nonce (1/3)"), 2, output)
        self.assertEqual(output.count("fresh nonce (2/3)"), 1, output)
        self.assertEqual(output.count("fresh nonce (3/3)"), 1, output)
        self.assert_fresh_mutex_attempts(attempts_file, 6)
        self.assertTrue((self.state / "claim-pr-visible").is_file())
        self.assertIsNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())

    def test_mutex_retry_budget_resets_for_new_ownership_transitions(self):
        activation_source = TEMPLATE.read_text(encoding="utf-8")
        claim_source = CLAIM_SCRIPT.read_text(encoding="utf-8")

        activation_recovery_reset = (
            "printf 'activation: recovered exact stale generated mutex %s after owner "
            "verification\\n' \"$mutex_observed\" >&2\n"
            "        MUTEX_EMPTY_RETRY_COUNT=0\n"
            "        acquire_activation_mutex"
        )
        activation_wait_reset = (
            "printf 'activation: the short mutex cleared after %s second(s); observing "
            "the durable lane\\n' \"$mutex_waited\" >&2\n"
            "          MUTEX_EMPTY_RETRY_COUNT=0\n"
            "          acquire_activation_mutex"
        )
        claim_recovery_reset = (
            "echo \"recovered exact stale generated mutex $observed after owner "
            "verification\" >&2\n"
            "        activation_mutex_empty_retry_count=0\n"
            "        acquire_activation_mutex"
        )
        self.assertIn(activation_recovery_reset, activation_source)
        self.assertIn(activation_wait_reset, activation_source)
        self.assertIn(claim_recovery_reset, claim_source)

    def test_slow_concurrent_activation_reports_the_exact_in_progress_revision(self):
        self.install_git_race_shim()
        second_checkout = self.root / "checkout-slow-two"
        self.git("clone", "-q", str(self.origin), str(second_checkout), cwd=self.root)
        race_barrier = self.root / "slow-race-barrier"
        race_barrier.mkdir()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self.run_activation,
                    checkout=checkout,
                    race_barrier=race_barrier,
                    race_runner=f"slow-{index}",
                    mutex_wait_seconds=1,
                    mutex_winner_delay=3,
                    exclusive_first_refresh=True,
                )
                for index, checkout in enumerate((self.checkout, second_checkout), start=1)
            ]
            results = [future.result() for future in futures]

        self.assertEqual([result.returncode for result in results], [3, 3])
        combined = "".join(result.stdout + result.stderr for result in results)
        observed_mutex = (
            f"package revision {self.package_sha} is in progress" in combined
            and "under exact activation mutex" in combined
        )
        observed_ready_lane = "package refresh PR #51 is ready" in combined
        self.assertTrue(observed_mutex or observed_ready_lane, combined)
        self.assertIn("PR #51", combined)
        self.assertEqual(
            self.suite_marker.read_text(encoding="utf-8").splitlines(),
            ["one updater"],
        )
        self.assertIsNone(self.remote_mutex_sha())

    def test_auto_deleted_merge_is_terminalized_before_a_newer_refresh(self):
        # Model a genuinely older refresh before the current one. Historical
        # PRs on the reusable head name must retain distinct immutable heads;
        # sharing one head would correctly be rejected as ambiguous.
        older_revision = self.package_sha
        older_base = self.git_output("rev-parse", "HEAD")
        self.git(
            "subtree",
            "add",
            "--prefix=docs/ai-team",
            str(self.source_bare),
            older_revision,
            "--squash",
        )
        older_message = (
            "AI Team package refresh\n\n"
            "AI-Team-Activation-Managed: true\n"
            f"AI-Team-Activation-Base: {older_base}\n"
            f"AI-Team-Package-Source: {bash_path(self.source_bare)}\n"
            "AI-Team-Package-Ref: package-mount\n"
            f"AI-Team-Package-Revision: {older_revision}\n"
        )
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "--file=-"],
            cwd=self.checkout,
            env=self.git_env(),
            input=older_message,
            text=True,
            check=True,
        )
        older_refresh_sha = self.git_output("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "HEAD:main")
        (self.state / "older-valid-head").write_text(
            f"{older_refresh_sha}\n", encoding="utf-8"
        )
        (self.state / "older-valid-merge").write_text(
            f"{older_refresh_sha}\n", encoding="utf-8"
        )
        (self.state / "older-valid-revision").write_text(
            f"{older_revision}\n", encoding="utf-8"
        )
        self.assertEqual(self.git_output("status", "--porcelain"), "")

        (self.source / "VERSION").write_text("first package revision\n", encoding="utf-8")
        self.git("add", "VERSION", cwd=self.source)
        self.git("commit", "-qm", "publish first package revision", cwd=self.source)
        self.git("push", "-q", "origin", "package-mount", cwd=self.source)
        self.package_sha = self.git_output("rev-parse", "HEAD", cwd=self.source)
        self.package_tree = self.git_output("rev-parse", "HEAD^{tree}", cwd=self.source)

        original = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(original.returncode, 3, original.stdout + original.stderr)
        refresh_sha = self.remote_update_sha()
        self.assertIsNotNone(refresh_sha)
        subprocess.run(
            ["git", "--git-dir", str(self.origin), "update-ref", "refs/heads/main", refresh_sha],
            env=self.git_env(),
            check=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.origin),
                "update-ref",
                "-d",
                f"refs/heads/{UPDATE_BRANCH}",
            ],
            env=self.git_env(),
            check=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.origin),
                "update-ref",
                "refs/pull/48/head",
                older_refresh_sha,
            ],
            env=self.git_env(),
            check=True,
        )
        (self.state / "pr-merged").write_text("yes\n", encoding="utf-8")
        self.git("pull", "-q", "--ff-only", "origin", "main")

        # The package source can advance again before any activation observes
        # GitHub's automatic branch deletion. R1 still has to reach task:done
        # from its own immutable head/merge metadata before R2 is considered.
        (self.source / "VERSION").write_text("second package revision\n", encoding="utf-8")
        self.git("add", "VERSION", cwd=self.source)
        self.git("commit", "-qm", "publish another package revision", cwd=self.source)
        self.git("push", "-q", "origin", "package-mount", cwd=self.source)
        self.package_sha = self.git_output("rev-parse", "HEAD", cwd=self.source)
        self.package_tree = self.git_output("rev-parse", "HEAD^{tree}", cwd=self.source)
        (self.state / "older-merged").write_text("yes\n", encoding="utf-8")
        (self.state / "valid-older-merged").write_text("yes\n", encoding="utf-8")
        (self.state / "pr-done-plus-review").write_text("yes\n", encoding="utf-8")
        (self.state / "active").write_text("yes\n", encoding="utf-8")

        blocked = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("refuses active task lanes: #77", blocked.stderr)
        self.assertFalse((self.state / "pr-terminal").exists())
        self.assertFalse((self.state / "pr-terminal-48").exists())
        self.assertIsNone(self.remote_mutex_sha())

        (self.state / "active").unlink()
        finalized = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(finalized.returncode, 3, finalized.stdout + finalized.stderr)
        self.assertIn(
            "terminalized auto-deleted merged package refresh PR #51",
            finalized.stdout,
            finalized.stdout + finalized.stderr,
        )
        self.assertIn(
            "terminalized auto-deleted merged package refresh PR #48",
            finalized.stdout,
            finalized.stdout + finalized.stderr,
        )
        self.assertTrue((self.state / "pr-terminal").is_file())
        self.assertTrue((self.state / "pr-terminal-48").is_file())
        self.assertIsNone(self.remote_mutex_sha())
        self.assertFalse(self.orient_marker.exists())
        self.assertNotIn("pr edit 49 --repo", self.gh_log.read_text(encoding="utf-8"))

    def test_malformed_failed_trailer_is_never_terminalized_with_or_without_branch(self):
        created = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(created.returncode, 3, created.stdout + created.stderr)
        malformed_sha = self.append_trailer_to_refresh(
            "AI-Team-Activation-Failed: false"
        )
        subprocess.run(
            ["git", "--git-dir", str(self.origin), "update-ref", "refs/heads/main", malformed_sha],
            env=self.git_env(),
            check=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.origin),
                "update-ref",
                "refs/pull/51/head",
                malformed_sha,
            ],
            env=self.git_env(),
            check=True,
        )
        (self.state / "pr-merged").write_text("yes\n", encoding="utf-8")
        self.git("pull", "-q", "--ff-only", "origin", "main")
        self.gh_log.write_text("", encoding="utf-8")

        lingering = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(lingering.returncode, 3, lingering.stdout + lingering.stderr)
        self.assertEqual(self.remote_update_sha(), malformed_sha)
        self.assertFalse((self.state / "pr-terminal").exists())
        self.assertNotIn("pr edit 51", self.gh_log.read_text(encoding="utf-8"))
        self.assertIsNone(self.remote_mutex_sha())

        (self.state / "pr-head").write_text(f"{malformed_sha}\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.origin),
                "update-ref",
                "-d",
                f"refs/heads/{UPDATE_BRANCH}",
            ],
            env=self.git_env(),
            check=True,
        )
        self.gh_log.write_text("", encoding="utf-8")
        deleted = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(deleted.returncode, 0, deleted.stdout + deleted.stderr)
        self.assertIn("head is not exact generated refresh state", deleted.stderr)
        self.assertFalse((self.state / "pr-terminal").exists())
        self.assertNotIn("pr edit 51", self.gh_log.read_text(encoding="utf-8"))
        self.assertIsNone(self.remote_mutex_sha())

    def test_dirty_checkout_refuses_without_creating_remote_state(self):
        original_head = self.git_output("rev-parse", "HEAD")

        unacknowledged = self.run_activation(exclusive_first_refresh=False)

        self.assertEqual(
            unacknowledged.returncode,
            1,
            unacknowledged.stdout + unacknowledged.stderr,
        )
        self.assertIn("first activation/migration is not mutex-compatible", unacknowledged.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())

        self.assert_caller_unchanged(original_head)

        self.git("push", "-q", "origin", f"HEAD:refs/heads/{UPDATE_BRANCH}")
        unknown_branch = self.remote_update_sha()

        unknown = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(unknown.returncode, 1, unknown.stdout + unknown.stderr)
        self.assertIn("not a completed activation-owned branch", unknown.stderr)
        self.assertEqual(self.remote_update_sha(), unknown_branch)
        self.assert_caller_unchanged(original_head)
        self.git("push", "-q", "origin", "--delete", UPDATE_BRANCH)

        self.git("switch", "-q", "-c", "forged-refresh")
        self.git(
            "commit",
            "--allow-empty",
            "-qm",
            "forged activation metadata",
            "-m",
            "AI-Team-Activation-Managed: true\n"
            "AI-Team-Activation-Base: fixture\n"
            f"AI-Team-Package-Source: {bash_path(self.source_bare)}\n"
            "AI-Team-Package-Ref: package-mount\n"
            f"AI-Team-Package-Revision: {self.package_sha}",
        )
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{UPDATE_BRANCH}")
        forged_sha = self.remote_update_sha()
        self.git("switch", "-q", "main")
        self.git("branch", "-D", "forged-refresh")

        forged = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(forged.returncode, 1, forged.stdout + forged.stderr)
        self.assertIn("invalid activation ancestry", forged.stderr)
        self.assertEqual(self.remote_update_sha(), forged_sha)
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)
        self.git("push", "-q", "origin", "--delete", UPDATE_BRANCH)

        self.git("switch", "-q", "-c", "forged-adopter-change")
        (self.checkout / "README.md").write_text("adopter work on generated branch\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git(
            "commit",
            "-qm",
            "generated-looking commit with adopter work",
            "-m",
            "AI-Team-Activation-Managed: true\n"
            f"AI-Team-Activation-Base: {original_head}\n"
            f"AI-Team-Package-Source: {bash_path(self.source_bare)}\n"
            "AI-Team-Package-Ref: package-mount\n"
            f"AI-Team-Package-Revision: {self.package_sha}",
        )
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{UPDATE_BRANCH}")
        adopter_work_sha = self.remote_update_sha()
        self.git("switch", "-q", "main")
        self.git("branch", "-D", "forged-adopter-change")

        adopter_work = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(adopter_work.returncode, 1, adopter_work.stdout + adopter_work.stderr)
        self.assertIn("changes adopter-owned paths outside docs/ai-team", adopter_work.stderr)
        self.assertEqual(self.remote_update_sha(), adopter_work_sha)
        self.assertIsNone(self.remote_mutex_sha())
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

        denied = self.run_activation(exclusive_first_refresh=True)

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

        unavailable = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(unavailable.returncode, 1, unavailable.stdout + unavailable.stderr)
        self.assertIn("cannot resolve approved package ref", unavailable.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assertEqual(self.git_output("rev-parse", "HEAD"), original_head)
        self.assertNotEqual(self.git_output("status", "--porcelain"), "")
        policy.write_text(original_policy, encoding="utf-8")
        self.assertEqual(self.git_output("status", "--porcelain"), "")

        (self.checkout / "README.md").write_text("dirty adopter\n", encoding="utf-8")

        result = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refuses a dirty checkout", result.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assertEqual(self.git_output("rev-parse", "HEAD"), original_head)
        self.assertFalse((self.state / "pr-number").exists())

    def test_distinct_same_repository_pushurl_drives_all_activation_writes(self):
        fetch_url = self.git_output("remote", "get-url", "origin")
        push_url = self.origin.resolve().as_uri()
        self.assertNotEqual(fetch_url, push_url)
        self.git("remote", "set-url", "--push", "origin", push_url)

        activated = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(activated.returncode, 3, activated.stdout + activated.stderr)
        self.assertIsNotNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        self.assertTrue((self.state / "ready").is_file())
        self.assertEqual(
            self.git_output("remote", "get-url", "--push", "origin"), push_url
        )

    def test_repository_selector_environment_fails_before_any_git_or_remote_state(self):
        original_head = self.git_output("rev-parse", "HEAD")
        original_config = self.git_output("config", "--local", "--list")
        common_dir = Path(self.git_output("rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = (self.checkout / common_dir).resolve()

        rejected = self.run_activation(git_dir=common_dir)

        self.assertEqual(rejected.returncode, 1, rejected.stdout + rejected.stderr)
        self.assertIn("GIT_DIR must be unset", rejected.stderr)
        self.assertEqual(self.git_output("config", "--local", "--list"), original_config)
        self.assert_caller_unchanged(original_head)
        self.assertIsNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        self.assertFalse(self.gh_log.exists())

    def test_issue_half_claim_and_pr_only_lane_each_refuse_the_update(self):
        original_head = self.git_output("rev-parse", "HEAD")
        (self.state / "active").write_text("yes\n", encoding="utf-8")

        issue_result = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(issue_result.returncode, 1, issue_result.stdout + issue_result.stderr)
        self.assertIn("refuses active task lanes: #77", issue_result.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assert_caller_unchanged(original_head)

        (self.state / "active").unlink()
        (self.state / "active-pr").write_text("yes\n", encoding="utf-8")
        pr_result = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(pr_result.returncode, 1, pr_result.stdout + pr_result.stderr)
        self.assertIn("refuses active task lanes: PR #88", pr_result.stderr)
        self.assertIsNone(self.remote_update_sha())
        self.assert_caller_unchanged(original_head)

        (self.state / "active-pr").unlink()
        (self.state / "activate-late-lane").write_text("yes\n", encoding="utf-8")
        late_lane = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(late_lane.returncode, 1, late_lane.stdout + late_lane.stderr)
        self.assertIn("task lane appeared concurrently: PR #88", late_lane.stderr)
        self.assertIsNotNone(self.remote_update_sha())
        self.assertEqual(
            (self.state / "pr-draft").read_text(encoding="utf-8").strip(), "true"
        )
        self.assert_caller_unchanged(original_head)

    def test_same_named_fork_and_wrong_base_refresh_prs_are_never_exempt_or_edited(self):
        original_head = self.git_output("rev-parse", "HEAD")

        (self.state / "fork-refresh-pr").write_text("yes\n", encoding="utf-8")
        fork_result = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(
            fork_result.returncode, 1, fork_result.stdout + fork_result.stderr
        )
        self.assertIn("refuses active task lanes: PR #89", fork_result.stderr)
        self.assertIsNone(self.remote_update_sha())
        fork_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("pr edit 89", fork_log)
        self.assertNotIn("pr ready 89", fork_log)
        self.assert_caller_unchanged(original_head)

        (self.state / "fork-refresh-pr").unlink()
        self.gh_log.write_text("", encoding="utf-8")
        (self.state / "other-base-refresh-pr").write_text("yes\n", encoding="utf-8")
        base_result = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(
            base_result.returncode, 1, base_result.stdout + base_result.stderr
        )
        self.assertIn("refuses active task lanes: PR #90", base_result.stderr)
        self.assertIsNone(self.remote_update_sha())
        base_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("pr edit 90", base_log)
        self.assertNotIn("pr ready 90", base_log)
        self.assert_caller_unchanged(original_head)

    def test_wrong_base_pr_protects_a_lingering_merged_refresh_before_cleanup(self):
        created = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(created.returncode, 3, created.stdout + created.stderr)
        lingering_sha = self.remote_update_sha()
        self.assertIsNotNone(lingering_sha)

        self.git("fetch", "-q", "origin", UPDATE_BRANCH)
        self.git("merge", "-q", "--ff-only", f"origin/{UPDATE_BRANCH}")
        self.git("push", "-q", "origin", "main")
        (self.state / "pr-merged").write_text("yes\n", encoding="utf-8")
        (self.state / "other-base-refresh-pr").write_text("yes\n", encoding="utf-8")
        self.gh_log.write_text("", encoding="utf-8")
        merged_head = self.git_output("rev-parse", "HEAD")

        blocked = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("refuses active task lanes: PR #90", blocked.stderr)
        self.assertEqual(self.remote_update_sha(), lingering_sha)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("pr edit 51", log)
        self.assertNotIn("pr edit 90", log)
        self.assertFalse((self.state / "pr-terminal").exists())
        self.assert_caller_unchanged(merged_head)

        (self.state / "other-base-refresh-pr").unlink()
        (self.state / "wrong-base-merged").write_text("yes\n", encoding="utf-8")
        self.gh_log.write_text("", encoding="utf-8")
        ambiguous_history = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(
            ambiguous_history.returncode,
            1,
            ambiguous_history.stdout + ambiguous_history.stderr,
        )
        self.assertIn("reused by multiple same-repository merged PRs", ambiguous_history.stderr)
        self.assertEqual(self.remote_update_sha(), lingering_sha)
        self.assertFalse((self.state / "pr-terminal").exists())
        ambiguous_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("pr edit 47", ambiguous_log)
        self.assertNotIn("pr edit 51", ambiguous_log)
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(merged_head)

        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.origin),
                "update-ref",
                "-d",
                f"refs/heads/{UPDATE_BRANCH}",
            ],
            env=self.git_env(),
            check=True,
        )
        self.gh_log.write_text("", encoding="utf-8")
        ambiguous_deleted = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(
            ambiguous_deleted.returncode,
            1,
            ambiguous_deleted.stdout + ambiguous_deleted.stderr,
        )
        self.assertIn("reused by multiple same-repository PRs", ambiguous_deleted.stderr)
        deleted_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("pr edit 47", deleted_log)
        self.assertNotIn("pr edit 51", deleted_log)
        self.assertFalse((self.state / "pr-terminal").exists())
        self.assertIsNone(self.remote_mutex_sha())

    def test_blocked_backlog_without_an_open_pr_does_not_freeze_activation(self):
        original_head = self.git_output("rev-parse", "HEAD")
        (self.state / "blocked-only").write_text("yes\n", encoding="utf-8")

        result = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertNotIn("#66", result.stderr)
        self.assertIsNotNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_unknown_mutex_and_unmerged_or_nonexact_refresh_fail_closed(self):
        original_head = self.git_output("rev-parse", "HEAD")
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{MUTEX_BRANCH}")
        unknown_mutex = self.remote_mutex_sha()

        locked = self.run_activation(mutex_wait_seconds=1, exclusive_first_refresh=True)

        self.assertEqual(locked.returncode, 1, locked.stdout + locked.stderr)
        self.assertIn("another activation or claim owns", locked.stderr)
        self.assertIn("never steals it", locked.stderr)
        self.assertEqual(self.remote_mutex_sha(), unknown_mutex)
        self.assertIsNone(self.remote_update_sha())
        self.assert_caller_unchanged(original_head)
        malformed_recovery = self.run_activation(
            recover_mutex_sha=unknown_mutex, exclusive_first_refresh=True
        )
        self.assertEqual(
            malformed_recovery.returncode,
            2,
            malformed_recovery.stdout + malformed_recovery.stderr,
        )
        self.assertIn("malformed or not generated state", malformed_recovery.stderr)
        self.assertEqual(self.remote_mutex_sha(), unknown_mutex)
        self.git("push", "-q", "origin", "--delete", MUTEX_BRANCH)

        stale_mutex = self.create_stale_generated_mutex()
        stale_refusal = self.run_activation(
            mutex_wait_seconds=1, exclusive_first_refresh=True
        )
        self.assertEqual(
            stale_refusal.returncode,
            3,
            stale_refusal.stdout + stale_refusal.stderr,
        )
        self.assertIn("under exact activation mutex", stale_refusal.stderr)
        self.assertEqual(self.remote_mutex_sha(), stale_mutex)

        created = self.run_activation(
            recover_mutex_sha=stale_mutex, exclusive_first_refresh=True
        )
        self.assertEqual(created.returncode, 3, created.stdout + created.stderr)
        self.assertIn("recovered exact stale generated mutex", created.stderr)
        self.assertIsNone(self.remote_mutex_sha())
        refresh_sha = self.remote_update_sha()
        self.assertIsNotNone(refresh_sha)
        (self.state / "pr-closed").write_text("yes\n", encoding="utf-8")

        closed = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(closed.returncode, 3, closed.stdout + closed.stderr)
        self.assertIn("has no open or matching merged PR", closed.stderr)
        self.assertIn(f"AI_TEAM_RECOVER_REFRESH_SHA={refresh_sha}", closed.stderr)
        self.assertEqual(self.remote_update_sha(), refresh_sha)
        self.assertIsNone(self.remote_mutex_sha())

        (self.state / "pr-closed").unlink()
        (self.state / "pr-merged").write_text("yes\n", encoding="utf-8")
        nonexact = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(nonexact.returncode, 1, nonexact.stdout + nonexact.stderr)
        self.assertIn("is not the package tree on origin/main", nonexact.stderr)
        self.assertEqual(self.remote_update_sha(), refresh_sha)
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_failed_mounted_suite_leaves_caller_unchanged_and_pr_draft(self):
        package_test = self.source / "scripts" / "test_smoke.py"
        package_test.write_text(
            textwrap.dedent(
                """\
                import os
                import unittest
                from pathlib import Path

                class Recoverable(unittest.TestCase):
                    def test_same_revision_can_resume(self):
                        state = Path(os.environ["ACTIVATION_TEST_GH_STATE"])
                        if (state / "force-suite-failure").exists():
                            self.fail("expected fixture failure")
                        with Path(os.environ["ACTIVATION_TEST_SUITE_MARKER"]).open(
                            "a", encoding="utf-8"
                        ) as marker:
                            marker.write("recovered updater\\n")
                """
            ),
            encoding="utf-8",
        )
        self.git("add", "scripts/test_smoke.py", cwd=self.source)
        self.git("commit", "-qm", "publish broken package", cwd=self.source)
        self.git("push", "-q", "origin", "package-mount", cwd=self.source)
        self.package_sha = self.git_output("rev-parse", "HEAD", cwd=self.source)
        self.package_tree = self.git_output("rev-parse", "HEAD^{tree}", cwd=self.source)
        original_head = self.git_output("rev-parse", "HEAD")
        (self.state / "force-suite-failure").write_text("yes\n", encoding="utf-8")

        result = self.run_activation(exclusive_first_refresh=True)

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
        failure_metadata = self.origin_output("log", "-1", "--format=%B", claim_sha)
        self.assertIn("AI-Team-Activation-Failed: true", failure_metadata)
        self.assertNotIn("AI-Team-Activation-Claim:", failure_metadata)
        self.assertEqual((self.state / "pr-draft").read_text(encoding="utf-8").strip(), "true")
        self.assertFalse((self.state / "ready").exists())
        failed_body = (self.state / "pr-body").read_text(encoding="utf-8")
        self.assertIn("Verification: pending", failed_body)
        self.assertNotIn("full mechanism suite passed", failed_body)

        (self.state / "force-suite-failure").unlink()
        retried = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(retried.returncode, 3, retried.stdout + retried.stderr)
        self.assertIn("resuming exact generated failure marker", retried.stderr)
        self.assertNotEqual(self.remote_update_sha(), claim_sha)
        self.assertTrue((self.state / "ready").is_file())
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_passing_suite_cannot_mutate_git_state_publish_orient_or_adopter_paths(self):
        original_head = self.git_output("rev-parse", "HEAD")
        common_dir = Path(self.git_output("rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = (self.checkout / common_dir).resolve()
        post_commit = common_dir / "hooks" / "post-commit"
        pre_push = common_dir / "hooks" / "pre-push"
        post_checkout = common_dir / "hooks" / "post-checkout"
        config_before = self.git_output("config", "--local", "--list")
        refs_before = self.git_output(
            "for-each-ref", "--format=%(refname)%09%(objectname)"
        )

        hook_only = self.run_activation(
            exclusive_first_refresh=True, suite_mutation="install-hook-only"
        )

        self.assertEqual(hook_only.returncode, 1, hook_only.stdout + hook_only.stderr)
        self.assertIn("modified its standalone repository", hook_only.stderr)
        self.assertFalse(post_commit.exists())
        self.assertFalse(pre_push.exists())
        self.assertFalse(post_checkout.exists())
        self.assertEqual(self.git_output("config", "--local", "--list"), config_before)
        self.assertEqual(
            self.git_output("for-each-ref", "--format=%(refname)%09%(objectname)"),
            refs_before,
        )
        self.assert_caller_unchanged(original_head)
        failed_sha = self.remote_update_sha()
        self.assertIsNotNone(failed_sha)
        failed_message = self.origin_output("log", "-1", "--format=%B", failed_sha)
        self.assertIn("AI-Team-Activation-Failed: true", failed_message)
        self.assertFalse((self.state / "ready").exists())

        # Owner-installed hooks are adopter state. Activation neither deletes
        # nor executes them for its generated commits and leased pushes.
        hook_body = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf 'activation hook ran\n' > "$ACTIVATION_TEST_HOOK_MARKER"
            """
        )
        for hook in (post_commit, pre_push, post_checkout):
            hook.write_text(hook_body, encoding="utf-8")
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        post_commit_bytes = post_commit.read_bytes()
        pre_push_bytes = pre_push.read_bytes()
        post_checkout_bytes = post_checkout.read_bytes()

        recovered = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(recovered.returncode, 3, recovered.stdout + recovered.stderr)
        self.assertFalse(self.hook_marker.exists())
        self.assertEqual(post_commit.read_bytes(), post_commit_bytes)
        self.assertEqual(pre_push.read_bytes(), pre_push_bytes)
        self.assertEqual(post_checkout.read_bytes(), post_checkout_bytes)
        verified_sha = self.remote_update_sha()
        self.assertIsNotNone(verified_sha)
        verified_message = self.origin_output("log", "-1", "--format=%B", verified_sha)
        self.assertIn("AI-Team-Activation-Managed: true", verified_message)
        self.assertIn(f"AI-Team-Activation-Base: {original_head}", verified_message)
        self.assertIn(f"AI-Team-Package-Revision: {self.package_sha}", verified_message)
        self.assertNotIn("AI-Team-Activation-Claim:", verified_message)
        self.assertNotIn("AI-Team-Activation-Failed:", verified_message)
        verified_parents = self.origin_output(
            "rev-list", "--parents", "-n", "1", verified_sha
        ).split()
        self.assertEqual(len(verified_parents), 2)
        self.assertEqual(
            self.origin_output("rev-parse", f"{verified_sha}^{{tree}}"),
            self.origin_output("rev-parse", f"{verified_parents[1]}^{{tree}}"),
        )
        self.assertEqual(
            self.origin_output("rev-parse", f"{verified_sha}:docs/ai-team"),
            self.package_tree,
        )
        self.assertEqual(
            self.origin_output(
                "diff",
                "--name-only",
                original_head,
                verified_sha,
                "--",
                ".",
                ":(exclude)docs/ai-team",
            ),
            "",
        )

        subprocess.run(
            ["git", "--git-dir", str(self.origin), "update-ref", "refs/heads/main", verified_sha],
            env=self.git_env(),
            check=True,
        )
        (self.state / "pr-merged").write_text("yes\n", encoding="utf-8")
        self.git("pull", "-q", "--ff-only", "origin", "main")
        merged_head = self.git_output("rev-parse", "HEAD")
        orient = self.checkout / "docs" / "ai-team" / "scripts" / "orient.sh"
        orient_bytes = orient.read_bytes()

        current_mutation = self.run_activation(suite_mutation="rewrite-current")

        self.assertEqual(
            current_mutation.returncode,
            1,
            current_mutation.stdout + current_mutation.stderr,
        )
        self.assertIn("modified its standalone repository", current_mutation.stderr)
        self.assertFalse(self.orient_marker.exists())
        self.assertEqual(orient.read_bytes(), orient_bytes)
        self.assertFalse((self.checkout / "docs" / "ai-team" / "UNTRACKED_EXECUTION_TARGET").exists())
        self.assertFalse(self.hook_marker.exists())
        self.assertEqual(post_commit.read_bytes(), post_commit_bytes)
        self.assertEqual(pre_push.read_bytes(), pre_push_bytes)
        self.assertEqual(post_checkout.read_bytes(), post_checkout_bytes)
        self.assert_caller_unchanged(merged_head)

        self.advance_package_source("POST_SUITE_MUTATION")
        outside_mutation = self.run_activation(suite_mutation="commit-outside")

        self.assertEqual(
            outside_mutation.returncode,
            1,
            outside_mutation.stdout + outside_mutation.stderr,
        )
        self.assertIn("modified its standalone repository", outside_mutation.stderr)
        self.assertFalse((self.checkout / "ADOPTER_SUITE_SENTINEL").exists())
        self.assertFalse(self.hook_marker.exists())
        self.assertEqual(post_commit.read_bytes(), post_commit_bytes)
        self.assertEqual(pre_push.read_bytes(), pre_push_bytes)
        self.assertEqual(post_checkout.read_bytes(), post_checkout_bytes)
        self.assert_caller_unchanged(merged_head)
        rejected_sha = self.remote_update_sha()
        self.assertIsNotNone(rejected_sha)
        rejected_message = self.origin_output("log", "-1", "--format=%B", rejected_sha)
        self.assertIn("AI-Team-Activation-Failed: true", rejected_message)
        missing_sentinel = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.origin),
                "cat-file",
                "-e",
                f"{rejected_sha}:ADOPTER_SUITE_SENTINEL",
            ],
            env=self.git_env(),
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(missing_sentinel.returncode, 0)
        self.assertEqual((self.state / "pr-draft").read_text(encoding="utf-8").strip(), "true")

    def test_final_push_and_label_failures_require_exact_recovery(self):
        original_head = self.git_output("rev-parse", "HEAD")
        reject_final = self.origin / "hooks" / "pre-receive"
        reject_final.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                while read -r old new ref; do
                  if [ "$ref" = "refs/heads/ai-team/package-refresh" ]; then
                    message=$(git show -s --format=%B "$new")
                    if ! printf '%s\\n' "$message" | grep -q '^AI-Team-Activation-Claim: '; then
                      echo "verified refresh push denied by fixture" >&2
                      exit 1
                    fi
                  fi
                done
                exit 0
                """
            ),
            encoding="utf-8",
        )
        reject_final.chmod(reject_final.stat().st_mode | stat.S_IXUSR)

        denied = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(denied.returncode, 1, denied.stdout + denied.stderr)
        self.assertIn("cannot publish the verified package refresh", denied.stderr)
        self.assertIn("could not convert", denied.stderr)
        orphan_claim = self.remote_update_sha()
        self.assertIsNotNone(orphan_claim)
        orphan_message = self.origin_output("log", "-1", "--format=%B", orphan_claim)
        self.assertIn("AI-Team-Activation-Claim:", orphan_message)
        self.assertIsNone(self.remote_mutex_sha())
        reject_final.unlink()

        refused = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertIn("AI_TEAM_RECOVER_REFRESH_SHA", refused.stderr)
        self.assertEqual(self.remote_update_sha(), orphan_claim)

        recovered = self.run_activation(
            recover_refresh_sha=orphan_claim, exclusive_first_refresh=True
        )
        self.assertEqual(recovered.returncode, 3, recovered.stdout + recovered.stderr)
        self.assertIn("owner selected exact pending refresh claim", recovered.stderr)
        verified_sha = self.remote_update_sha()
        self.assertNotEqual(verified_sha, orphan_claim)
        self.assertTrue((self.state / "ready").is_file())

        (self.state / "inject-blocked-on-ready").write_text("yes\n", encoding="utf-8")
        bad_labels = self.run_activation(
            recover_refresh_sha=verified_sha, exclusive_first_refresh=True
        )
        self.assertEqual(bad_labels.returncode, 1, bad_labels.stdout + bad_labels.stderr)
        self.assertIn("did not reach the exact", bad_labels.stderr)
        failed_label_sha = self.remote_update_sha()
        self.assertNotEqual(failed_label_sha, verified_sha)
        self.assertTrue((self.state / "final-blocked").exists())
        (self.state / "inject-blocked-on-ready").unlink()

        label_recovery = self.run_activation(
            recover_refresh_sha=failed_label_sha, exclusive_first_refresh=True
        )
        self.assertEqual(
            label_recovery.returncode,
            3,
            label_recovery.stdout + label_recovery.stderr,
        )
        self.assertFalse((self.state / "final-blocked").exists())
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_verified_pr_restart_requires_exact_recovery_and_repairs_task_state(self):
        original_head = self.git_output("rev-parse", "HEAD")
        (self.state / "fail-next-ready").write_text("yes\n", encoding="utf-8")

        interrupted = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(interrupted.returncode, 1, interrupted.stdout + interrupted.stderr)
        self.assertIn("could not mark PR #51 ready", interrupted.stderr)
        verified_sha = self.remote_update_sha()
        self.assertIsNotNone(verified_sha)
        self.assertEqual((self.state / "pr-draft").read_text().strip(), "true")
        verified_revision = self.package_sha
        newer_revision = self.advance_package_source("AFTER_VERIFIED_DRAFT")

        refused = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertIn("is not in exact ready/task:review state", refused.stderr)
        self.assertIn(verified_revision, refused.stderr)
        self.assertIn(newer_revision, refused.stderr)
        self.assertIn(f"AI_TEAM_RECOVER_REFRESH_SHA={verified_sha}", refused.stderr)
        self.assertEqual(self.remote_update_sha(), verified_sha)

        recovered = self.run_activation(
            recover_refresh_sha=verified_sha, exclusive_first_refresh=True
        )
        self.assertEqual(recovered.returncode, 3, recovered.stdout + recovered.stderr)
        repaired_sha = self.remote_update_sha()
        self.assertNotEqual(repaired_sha, verified_sha)
        self.assertTrue((self.state / "ready").is_file())
        repaired_message = self.origin_output("log", "-1", "--format=%B", repaired_sha)
        self.assertIn(f"AI-Team-Package-Revision: {newer_revision}", repaired_message)

        (self.state / "extra-blocked").write_text("yes\n", encoding="utf-8")
        contradictory = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(
            contradictory.returncode,
            3,
            contradictory.stdout + contradictory.stderr,
        )
        self.assertIn("contradictory task labels", contradictory.stderr)
        self.assertIn(f"AI_TEAM_RECOVER_REFRESH_SHA={repaired_sha}", contradictory.stderr)

        normalized = self.run_activation(
            recover_refresh_sha=repaired_sha, exclusive_first_refresh=True
        )
        self.assertEqual(normalized.returncode, 3, normalized.stdout + normalized.stderr)
        self.assertFalse((self.state / "extra-blocked").exists())
        self.assertEqual((self.state / "pr-draft").read_text().strip(), "false")
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_ready_verified_refresh_is_drafted_before_a_newer_claim_is_pushed(self):
        original_head = self.git_output("rev-parse", "HEAD")
        first = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(first.returncode, 3, first.stdout + first.stderr)
        old_verified_sha = self.remote_update_sha()
        self.assertIsNotNone(old_verified_sha)
        self.assertEqual((self.state / "pr-draft").read_text().strip(), "false")

        newer_revision = self.advance_package_source("AFTER_READY_REFRESH")
        (self.state / "fail-next-undo").write_text("yes\n", encoding="utf-8")
        failed_draft = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(
            failed_draft.returncode,
            1,
            failed_draft.stdout + failed_draft.stderr,
        )
        self.assertIn("could not return exact refresh PR #51 to draft", failed_draft.stderr)
        self.assertEqual(self.remote_update_sha(), old_verified_sha)
        self.assertEqual((self.state / "pr-draft").read_text().strip(), "false")

        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                while read -r old new ref; do
                  if [ "$ref" = "refs/heads/ai-team/package-refresh" ] &&
                     [ "$(cat "$ACTIVATION_TEST_GH_STATE/pr-draft")" != "true" ]; then
                    echo "fixture rejected a refresh push before the PR became draft" >&2
                    exit 1
                  fi
                done
                exit 0
                """
            ),
            encoding="utf-8",
        )
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        self.gh_log.write_text("", encoding="utf-8")

        refreshed = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(refreshed.returncode, 3, refreshed.stdout + refreshed.stderr)
        self.assertNotEqual(self.remote_update_sha(), old_verified_sha)
        message = self.origin_output("log", "-1", "--format=%B", self.remote_update_sha())
        self.assertIn(f"AI-Team-Package-Revision: {newer_revision}", message)
        self.assertIn("pr ready 51 --repo example/adopter --undo", self.gh_log.read_text())
        self.assertEqual((self.state / "pr-draft").read_text().strip(), "false")
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_no_pr_orphan_claim_requires_exact_recovery(self):
        original_head = self.git_output("rev-parse", "HEAD")
        recorded_revision = self.package_sha
        orphan_claim = self.create_orphan_refresh_claim()

        (self.source / "AFTER_ORPHAN").write_text(
            "newer approved package\n", encoding="utf-8"
        )
        self.git("add", "AFTER_ORPHAN", cwd=self.source)
        self.git("commit", "-qm", "advance after orphan claim", cwd=self.source)
        self.git("push", "-q", "origin", "package-mount", cwd=self.source)
        self.package_sha = self.git_output("rev-parse", "HEAD", cwd=self.source)
        self.package_tree = self.git_output("rev-parse", "HEAD^{tree}", cwd=self.source)
        self.assertNotEqual(self.package_sha, recorded_revision)

        refused = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertIn(f"claimed for recorded package revision {recorded_revision}", refused.stderr)
        self.assertIn(f"recover it to {self.package_sha}", refused.stderr)
        self.assertIn(f"AI_TEAM_RECOVER_REFRESH_SHA={orphan_claim}", refused.stderr)
        self.assertEqual(self.remote_update_sha(), orphan_claim)
        self.assertFalse((self.state / "pr-number").exists())
        self.assertIsNone(self.remote_mutex_sha())

        recovered = self.run_activation(
            recover_refresh_sha=orphan_claim, exclusive_first_refresh=True
        )

        self.assertEqual(recovered.returncode, 3, recovered.stdout + recovered.stderr)
        self.assertIn("owner selected exact orphan refresh claim", recovered.stderr)
        self.assertNotEqual(self.remote_update_sha(), orphan_claim)
        recovered_metadata = self.origin_output(
            "log", "-1", "--format=%B", self.remote_update_sha()
        )
        self.assertIn(f"AI-Team-Package-Revision: {self.package_sha}", recovered_metadata)
        self.assertTrue((self.state / "ready").is_file())
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_open_pending_claim_remains_the_only_owner_when_source_advances(self):
        original_head = self.git_output("rev-parse", "HEAD")
        recorded_revision = self.package_sha
        pending_sha = self.create_orphan_refresh_claim()
        self.expose_refresh_pr(draft=True)
        newer_revision = self.advance_package_source("AFTER_LIVE_CLAIM")

        contender = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(
            contender.returncode, 3, contender.stdout + contender.stderr
        )
        self.assertIn(f"PR #51 owns pending claim {pending_sha}", contender.stderr)
        self.assertIn(f"recorded revision {recorded_revision}", contender.stderr)
        self.assertIn(f"recover it to {newer_revision}", contender.stderr)
        self.assertIn(f"AI_TEAM_RECOVER_REFRESH_SHA={pending_sha}", contender.stderr)
        self.assertEqual(self.remote_update_sha(), pending_sha)
        self.assertFalse(self.suite_marker.exists())
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

        recovered = self.run_activation(
            recover_refresh_sha=pending_sha, exclusive_first_refresh=True
        )

        self.assertEqual(recovered.returncode, 3, recovered.stdout + recovered.stderr)
        self.assertIn("owner selected exact pending refresh claim", recovered.stderr)
        self.assertNotEqual(self.remote_update_sha(), pending_sha)
        recovered_message = self.origin_output(
            "log", "-1", "--format=%B", self.remote_update_sha()
        )
        self.assertIn(f"AI-Team-Package-Revision: {newer_revision}", recovered_message)
        self.assertTrue((self.state / "ready").exists())
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_generated_markers_never_authorize_overwriting_package_path_sentinels(self):
        original_head = self.git_output("rev-parse", "HEAD")

        forged_claim = self.create_forged_refresh_with_package_sentinel(
            "AI-Team-Activation-Claim: 0123456789abcdef0123456789abcdef\n"
        )
        self.expose_refresh_pr(draft=True)
        claim_result = self.run_activation(
            recover_refresh_sha=forged_claim, exclusive_first_refresh=True
        )
        self.assertEqual(
            claim_result.returncode, 1, claim_result.stdout + claim_result.stderr
        )
        self.assertIn("malformed generated claim/failure shape", claim_result.stderr)
        self.assertEqual(self.remote_update_sha(), forged_claim)
        self.assertIsNone(self.remote_mutex_sha())

        self.git("push", "-q", "origin", "--delete", UPDATE_BRANCH)
        (self.state / "pr-number").unlink()
        (self.state / "pr-draft").unlink()
        forged_failed = self.create_forged_refresh_with_package_sentinel(
            "AI-Team-Activation-Failed: true\n"
        )
        failed_result = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(
            failed_result.returncode, 1, failed_result.stdout + failed_result.stderr
        )
        self.assertIn("malformed generated claim/failure shape", failed_result.stderr)
        self.assertEqual(self.remote_update_sha(), forged_failed)
        self.assertIsNone(self.remote_mutex_sha())

        self.git("push", "-q", "origin", "--delete", UPDATE_BRANCH)
        forged_verified = self.create_forged_refresh_with_package_sentinel()
        self.expose_refresh_pr(draft=False)
        (self.state / "verified-edit").write_text("yes\n", encoding="utf-8")
        verified_result = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(
            verified_result.returncode,
            1,
            verified_result.stdout + verified_result.stderr,
        )
        self.assertIn("does not exactly match its recorded package revision", verified_result.stderr)
        self.assertEqual(self.remote_update_sha(), forged_verified)
        self.assertEqual((self.state / "pr-draft").read_text().strip(), "false")
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_same_tree_new_revision_can_recover_an_older_orphan(self):
        self.git(
            "subtree",
            "add",
            "--prefix=docs/ai-team",
            str(self.source_bare),
            self.package_sha,
            "--squash",
        )
        self.git("push", "-q", "origin", "main")
        original_head = self.git_output("rev-parse", "HEAD")
        recorded_revision = self.package_sha
        orphan_claim = self.create_orphan_refresh_claim()

        self.git("commit", "--allow-empty", "-qm", "publish same-tree revision", cwd=self.source)
        self.git("push", "-q", "origin", "package-mount", cwd=self.source)
        self.package_sha = self.git_output("rev-parse", "HEAD", cwd=self.source)
        same_tree = self.git_output("rev-parse", "HEAD^{tree}", cwd=self.source)
        self.assertNotEqual(self.package_sha, recorded_revision)
        self.assertEqual(same_tree, self.package_tree)

        refused = self.run_activation(exclusive_first_refresh=True)

        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertIn(f"exact generated refresh {orphan_claim}", refused.stderr)
        self.assertIn(f"AI_TEAM_RECOVER_REFRESH_SHA={orphan_claim}", refused.stderr)
        self.assertEqual(self.remote_update_sha(), orphan_claim)

        recovered = self.run_activation(
            recover_refresh_sha=orphan_claim, exclusive_first_refresh=True
        )

        self.assertEqual(recovered.returncode, 3, recovered.stdout + recovered.stderr)
        self.assertIn(f"recorded revision {recorded_revision}", recovered.stderr)
        recovered_metadata = self.origin_output(
            "log", "-1", "--format=%B", self.remote_update_sha()
        )
        self.assertIn(f"AI-Team-Package-Revision: {self.package_sha}", recovered_metadata)
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

    def test_verified_no_pr_refresh_requires_exact_recovery(self):
        original_head = self.git_output("rev-parse", "HEAD")
        created = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(created.returncode, 3, created.stdout + created.stderr)
        verified_sha = self.remote_update_sha()
        self.assertIsNotNone(verified_sha)

        # Simulate a verified generated branch whose PR was deleted/closed
        # without a merge. It is not deletion-safe, but an owner can select
        # the exact immutable branch tip and rebuild its draft lane.
        for state_name in (
            "pr-number",
            "pr-draft",
            "pr-body",
            "ready",
            "pr-create-success",
        ):
            (self.state / state_name).unlink(missing_ok=True)
        self.gh_log.write_text("", encoding="utf-8")

        refused = self.run_activation(exclusive_first_refresh=True)
        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertIn("has no open or matching merged PR", refused.stderr)
        self.assertIn(f"AI_TEAM_RECOVER_REFRESH_SHA={verified_sha}", refused.stderr)
        self.assertEqual(self.remote_update_sha(), verified_sha)
        refused_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("pr edit", refused_log)
        self.assertNotIn("label create", refused_log)

        recovered = self.run_activation(
            recover_refresh_sha=verified_sha, exclusive_first_refresh=True
        )
        self.assertEqual(recovered.returncode, 3, recovered.stdout + recovered.stderr)
        self.assertIn("selected exact verified refresh", recovered.stderr)
        self.assertTrue((self.state / "ready").is_file())
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)

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

        conflict = self.run_activation(exclusive_first_refresh=True)

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

        result = self.run_activation(checkout=safe_checkout, exclusive_first_refresh=True)

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

    def test_current_mutex_client_refreshes_without_legacy_owner_override(self):
        self.git(
            "subtree",
            "add",
            "--prefix=docs/ai-team",
            str(self.source_bare),
            self.package_sha,
            "--squash",
        )
        self.git("push", "-q", "origin", "main")
        original_head = self.git_output("rev-parse", "HEAD")

        (self.source / "CURRENT_CLIENT_UPDATE").write_text(
            "new package content\n", encoding="utf-8"
        )
        self.git("add", "CURRENT_CLIENT_UPDATE", cwd=self.source)
        self.git("commit", "-qm", "advance current mutex package", cwd=self.source)
        self.git("push", "-q", "origin", "package-mount", cwd=self.source)
        self.package_sha = self.git_output("rev-parse", "HEAD", cwd=self.source)
        self.package_tree = self.git_output("rev-parse", "HEAD^{tree}", cwd=self.source)

        result = self.run_activation()

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertNotIn("first activation/migration is not mutex-compatible", result.stderr)
        self.assertNotIn("owner acknowledged an exclusive first refresh", result.stdout)
        self.assertIsNotNone(self.remote_update_sha())
        self.assertIsNone(self.remote_mutex_sha())
        self.assert_caller_unchanged(original_head)


if __name__ == "__main__":
    unittest.main()
