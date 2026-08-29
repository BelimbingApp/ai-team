import os
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path

HELPER = Path(__file__).with_name("_default_branch.sh")


class DefaultBranchResolutionTest(unittest.TestCase):
    """The package must not assume `main`.

    Every fixture in this suite hardcoded `main` before #445, which is exactly
    why 24 hardcoded references survived across five scripts. These tests are
    written against a `master`-default repository on purpose: if the resolver
    regresses to a constant, they fail.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)
        self.remote = self.base / "remote.git"
        self.work = self.base / "work"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(
            ["git", "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/master"],
            check=True,
        )
        subprocess.run(["git", "init", "-q", "-b", "master", str(self.work)], check=True)
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test Agent")
        (self.work / "a.txt").write_text("a\n", encoding="utf-8")
        self._git("add", "a.txt")
        self._git("commit", "-q", "-m", "base")
        self._git("remote", "add", "origin", str(self.remote))
        self._git("push", "-q", "-u", "origin", "master")

        # A deterministic gh for every script invocation in this class. Without
        # it the resolver could reach the developer's real gh and answer for
        # whatever repository this checkout happens to point at.
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
                    # Empty means "no halt". [] reads as an active halt and
                    # orient.sh exits before the section under test.
                    if [[ "$*" == *"--label ops:halt"* ]]; then printf ''
                    else printf '[]\\n'; fi
                    ;;
                  *) printf '[]\\n' ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.dir.cleanup()

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.work, check=True, capture_output=True)

    def resolve(self, env_extra=None, cwd=None):
        env = os.environ.copy()
        env.pop("AI_TEAM_BASE_BRANCH", None)
        env.update(env_extra or {})
        # Through the shared boundary like the other two: bare "bash" can find
        # WSL's bash rather than Git Bash on Windows, which is what
        # _bash_executable() exists to settle.
        script = f'source {bash_path(HELPER)}\nai_team_default_branch\n'
        result = run_with_bash_path(
            ["bash", "-c", script],
            stub_directory=self.bin,
            env=env,
            cwd=str(cwd or self.work),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_a_master_default_repository_resolves_master_not_main(self):
        self.assertEqual(self.resolve(), "master")

    def test_an_explicit_override_wins_over_observed_state(self):
        self.assertEqual(self.resolve({"AI_TEAM_BASE_BRANCH": "release"}), "release")

    def test_a_stale_origin_head_is_rejected_rather_than_trusted(self):
        # origin/HEAD naming a branch that no longer exists on origin is a real
        # observed condition, not a hypothetical: it is what one adopting
        # checkout carried while its actual default had moved on.
        subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/deleted-branch"],
            cwd=self.work, check=True, capture_output=True,
        )
        self.assertEqual(self.resolve(), "master")

    def test_it_falls_back_to_main_when_nothing_is_observable(self):
        empty = self.base / "empty"
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        self.assertEqual(self.resolve(cwd=empty), "main")

    def test_no_shipped_script_hardcodes_the_default_branch(self):
        # The regression this whole change exists to prevent. A new script that
        # writes origin/main directly fails here rather than at an adopter's
        # first orient.
        offenders = []
        for script in sorted(Path(__file__).parent.glob("*.sh")):
            # The resolver is the single place allowed to name the constant —
            # it owns the final fallback. Everywhere else must go through it.
            if script.name == "_default_branch.sh":
                continue
            text = script.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if "origin/main" in line or "origin main" in line:
                    offenders.append(f"{script.name}:{number}: {line.strip()}")
                # A bare comparison against the literal branch name is the form
                # that escaped the first version of this test: cleanup.sh
                # protected `main` from deletion and orient.sh keyed its
                # freshness path on `main`, neither of which contains
                # "origin/main". On a master-default repository the first would
                # have offered to delete `master`.
                if re.search(r'=\s*"main"|"main"\s*=|=\s*\x27main\x27', line):
                    offenders.append(f"{script.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "hardcoded default branch:\n" + "\n".join(offenders))

    def test_no_shipped_script_hardcodes_a_particular_repository(self):
        offenders = []
        for script in sorted(Path(__file__).parent.glob("*.sh")):
            for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if "BelimbingApp/belimbing" in line:
                    offenders.append(f"{script.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "hardcoded repository:\n" + "\n".join(offenders))


    # --- behavioural regressions for the literal-branch assumptions that a
    # --- static text scan alone did not catch (found in review of #446).

    def _run(self, script, cwd, env_extra=None):
        # Route through run_with_bash_path/bash_path rather than passing a raw
        # str(Path). On Windows the raw form is a `D:\...` argument that Git Bash
        # surfaces as BASH_SOURCE[0], so claim.sh cannot cd to its own directory
        # and cleanup.sh continues with an empty helper and an empty $BASE — the
        # test then passes or fails for reasons unrelated to what it asserts.
        env = os.environ.copy()
        env.pop("AI_TEAM_BASE_BRANCH", None)
        env.update(env_extra or {})
        return run_with_bash_path(
            ["bash", bash_path(Path(__file__).with_name(script))],
            stub_directory=self.bin,
            env=env,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cleanup_never_offers_to_delete_a_non_main_default_branch(self):
        # cleanup.sh protected the literal `main`, so on this repository it
        # would have listed `master` as deletable and --yes would have deleted
        # the default branch. That is data loss, not a cosmetic branch-name bug.
        self._git("checkout", "-q", "-b", "agent/x-issue-1")
        (self.work / "b.txt").write_text("b\n", encoding="utf-8")
        self._git("add", "b.txt")
        self._git("commit", "-q", "-m", "lane")
        self._git("checkout", "-q", "master")
        self._git("merge", "-q", "--ff-only", "agent/x-issue-1")
        self._git("push", "-q", "origin", "master")
        result = self._run("cleanup.sh", self.work)
        self.assertEqual(result.returncode, 0, result.stderr)
        merged = result.stdout.split("== unmerged")[0]
        self.assertNotIn("  master", merged, f"cleanup offered to delete the default branch:\n{result.stdout}")
        self.assertIn("agent/x-issue-1", merged, result.stdout)

    def test_orient_reports_freshness_for_a_non_main_default_checkout(self):
        # orient.sh keyed its default-branch freshness path on the literal
        # `main`, so a stale `master` checkout silently took the lane path and
        # lost the behind-count and stale-files warning.
        publisher = self.base / "publisher"
        subprocess.run(["git", "clone", "-q", "-b", "master", str(self.remote), str(publisher)], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=publisher, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=publisher, check=True)
        (publisher / "new.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "add", "new.txt"], cwd=publisher, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance"], cwd=publisher, check=True)
        subprocess.run(["git", "push", "-q", "origin", "master"], cwd=publisher, check=True)
        self._git("fetch", "-q", "origin", "master")

        result = self._run("orient.sh", self.work)
        self.assertIn("master: BEHIND origin/master by 1 commit", result.stdout, result.stdout + result.stderr)


class NestedRepositoryLanePlacementTest(unittest.TestCase):
    """A lane worktree must never be created inside the host working tree.

    --show-superproject-working-tree only answers for a registered submodule and
    is EMPTY for an ordinary independent repository nested inside another, which
    is exactly the private-Extension shape. Relying on it left lanes at
    <host>/app/Extensions/.ai-team-lanes — inside the path the host application
    scans for modules, where a stray checkout can register phantom modules.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)
        env = os.environ.copy()
        env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        self.env = env

        self.host = self.base / "host"
        (self.host / "app" / "Extensions").mkdir(parents=True)
        self._run(["git", "init", "-q", "-b", "main", str(self.host)])
        (self.host / "README").write_text("host\n", encoding="utf-8")
        self._run(["git", "add", "-A"], cwd=self.host)
        self._run(["git", "commit", "-q", "-m", "host"], cwd=self.host)

        # An ordinary nested repository — deliberately NOT a submodule.
        self.bare = self.base / "ext.git"
        self._run(["git", "init", "-q", "--bare", str(self.bare)])
        self._run(["git", "--git-dir", str(self.bare), "symbolic-ref", "HEAD", "refs/heads/main"])
        self.ext = self.host / "app" / "Extensions" / "SbGroup"
        self._run(["git", "init", "-q", "-b", "main", str(self.ext)])
        (self.ext / "e.txt").write_text("e\n", encoding="utf-8")
        self._run(["git", "add", "-A"], cwd=self.ext)
        self._run(["git", "commit", "-q", "-m", "ext"], cwd=self.ext)
        self._run(["git", "remote", "add", "origin", str(self.bare)], cwd=self.ext)
        self._run(["git", "push", "-q", "-u", "origin", "main"], cwd=self.ext)

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
                  "issue view") printf '%s\\n' '{"state":"OPEN","labels":[],"title":"t","url":"u"}' ;;
                  "pr list") printf '[]\\n' ;;
                  "label list") printf '[]\\n' ;;
                  "label create") exit 0 ;;
                  "pr create") printf 'https://example/pull/1\\n' ;;
                  "pr edit"|"issue edit") exit 0 ;;
                  *) printf '[]\\n' ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, args, cwd=None):
        return subprocess.run(args, cwd=str(cwd) if cwd else None,
                              env=getattr(self, "env", None), check=True, capture_output=True)

    def test_the_lane_worktree_is_created_outside_the_host_working_tree(self):
        env = self.env.copy()
        env["CLAIM_AGENT"] = "opus-5-b"
        result = run_with_bash_path(
            ["bash", bash_path(Path(__file__).with_name("claim.sh")), "7"],
            stub_directory=self.bin,
            env=env,
            cwd=str(self.ext),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        reported = [l for l in result.stdout.splitlines() if l.startswith("worktree:")]
        self.assertTrue(reported, result.stdout)
        lane = Path(reported[0].split("worktree:", 1)[1].strip()).resolve()
        host = self.host.resolve()
        self.assertFalse(
            str(lane).startswith(str(host) + os.sep),
            f"lane worktree {lane} is inside the host working tree {host}; "
            "a checkout there can register phantom modules in the composed app",
        )
        self.assertTrue(lane.exists(), f"claim reported {lane} but it does not exist")


if __name__ == "__main__":
    unittest.main()
