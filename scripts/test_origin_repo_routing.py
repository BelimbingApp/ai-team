import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


SCRIPTS = {
    "claim.sh": (["17"], {"CLAIM_AGENT": "routing-test"}),
    "hold.sh": (["review", "add", "17"], {"CLAIM_AGENT": "routing-test"}),
    "land.sh": (["17", "a" * 40], {"LAND_AGENT": "routing-test"}),
    "orient.sh": ([], {}),
    "ready.sh": (["17"], {"CLAIM_AGENT": "routing-test"}),
    "review_gate.sh": (["17"], {}),
    "rerun-review-check.sh": (["17"], {}),
}


class OriginRepositoryRoutingTest(unittest.TestCase):
    """Every board reader/writer must ignore gh's ambient repository choice."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)], check=True)
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Routing Test")
        (self.work / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "fixture")
        self.push_origin = self.base / "origin.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.push_origin)],
            check=True,
        )
        self.git("remote", "add", "origin", "https://github.com/example/origin.git")
        self.git("remote", "set-url", "--push", "origin", str(self.push_origin))

        # This is the configuration that makes bare `gh repo view` resolve an
        # ambient repository instead of the repository named by origin. Keep a
        # local origin/main ref so default-branch discovery never needs network.
        self.git("config", "remote.origin.gh-resolved", "base")
        head = self.git_output("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", head)

        self.bin = self.base / "bin"
        self.bin.mkdir()
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git is required")
        git = self.bin / "git"
        git.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                # orient.sh refreshes origin before printing the board. The
                # fixture already has origin/main; suppress a real GitHub fetch.
                if [ "${1:-}" = "fetch" ]; then exit 0; fi
                # claim.sh now takes the real shared mutex before its first
                # GitHub read. Keep the canonical origin URL for routing, but
                # perform mutex readback against the local same-repository push
                # target so this fixture never contacts GitHub.
                if [ "${1:-}" = "ls-remote" ]; then
                  shift
                  rewritten=()
                  for argument in "$@"; do
                    [ "$argument" != "origin" ] || argument="$ORIGIN_ROUTING_PUSH_ORIGIN"
                    rewritten+=("$argument")
                  done
                  exec "$ORIGIN_ROUTING_REAL_GIT" ls-remote "${rewritten[@]}"
                fi
                exec "$ORIGIN_ROUTING_REAL_GIT" "$@"
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        git.chmod(git.stat().st_mode | stat.S_IXUSR)

        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                case "$1 $2" in
                  "repo view")
                    if [ "${3:-}" = "example/origin" ]; then
                      if [[ "$*" == *defaultBranchRef* ]]; then printf 'main\\n'
                      else printf 'example/origin\\n'; fi
                    else
                      # Model the wrong answer caused by gh-resolved when no
                      # explicit repository argument is supplied.
                      printf 'example/ambient\\n'
                    fi
                    ;;
                  "issue view"|"pr view")
                    [[ "$*" == *"--repo example/origin"* ]] || exit 18
                    exit 19
                    ;;
                  "issue list")
                    scoped_repo=''
                    previous=''
                    for argument in "$@"; do
                      [ "$previous" = '--repo' ] && scoped_repo="$argument"
                      previous="$argument"
                    done
                    [ "$scoped_repo" = 'example/origin' ] || exit 18
                    if [[ "$*" == *"--label ops:halt"* ]] && [ "${ORIGIN_ROUTING_SCRIPT:-}" = "claim.sh" ]; then printf ''
                    else printf '  ORIGIN ROUTING PROBE\\n'; fi
                    ;;
                  "pr list") printf '' ;;
                  "api"*)
                    # A REST pull read embeds the repository in the path, not
                    # in --repo; the probe must still catch ambient scoping.
                    for argument in "$@"; do
                      case "$argument" in
                        repos/*/pulls/*)
                          [[ "$argument" == repos/example/origin/* ]] || exit 18
                          ;;
                      esac
                    done
                    exit 19
                    ;;
                  *) printf '' ;;
                esac
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

        self.env = os.environ.copy()
        self.env.pop("AI_TEAM_BASE_BRANCH", None)
        self.env.pop("REVIEW_GATE_INPUT", None)
        self.env["ORIGIN_ROUTING_REAL_GIT"] = bash_path(Path(real_git))
        self.env["ORIGIN_ROUTING_PUSH_ORIGIN"] = bash_path(self.push_origin)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.work, check=True, capture_output=True
        )

    def git_output(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.work, check=True, capture_output=True,
            text=True, encoding="utf-8",
        ).stdout.strip()

    def assert_script_scopes_github_to_origin(self, name: str) -> None:
        scripts_dir = Path(__file__).parent
        arguments, extra_env = SCRIPTS[name]
        env = self.env.copy()
        env.update(extra_env)
        env["ORIGIN_ROUTING_SCRIPT"] = name
        result = run_with_bash_path(
            ["bash", bash_path(scripts_dir / name), *arguments],
            stub_directory=self.bin,
            env=env,
            cwd=self.work,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        output = result.stdout + result.stderr
        if name == "orient.sh":
            self.assertEqual(result.returncode, 3, output)
            self.assertIn("ORIGIN ROUTING PROBE", output)
        else:
            self.assertEqual(result.returncode, 2, output)
            self.assertIn("from example/origin", output)
        self.assertNotIn("example/ambient", output)

    def test_claim_scopes_github_to_origin(self):
        self.assert_script_scopes_github_to_origin("claim.sh")
        mutex = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.push_origin),
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/ai-team/claim-refresh-mutex",
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(mutex.returncode, 0)

    def test_hold_scopes_github_to_origin(self):
        self.assert_script_scopes_github_to_origin("hold.sh")

    def test_land_scopes_github_to_origin(self):
        self.assert_script_scopes_github_to_origin("land.sh")

    def test_orient_scopes_github_to_origin(self):
        self.assert_script_scopes_github_to_origin("orient.sh")

    def test_ready_scopes_github_to_origin(self):
        self.assert_script_scopes_github_to_origin("ready.sh")

    def test_review_gate_scopes_github_to_origin(self):
        self.assert_script_scopes_github_to_origin("review_gate.sh")

    def test_rerun_review_check_scopes_github_to_origin(self):
        self.assert_script_scopes_github_to_origin("rerun-review-check.sh")


if __name__ == "__main__":
    unittest.main()
