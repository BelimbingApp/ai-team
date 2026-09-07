"""gate.sh under an exhausted shared GraphQL budget (#103).

Self-contained on purpose: the bare repository, the git shim, the gh stub and
every fixture live in this file, so it stands up when it is run alone.

The shared account's GraphQL budget is 5000/hour across every agent and every
board, and it has run out twice in an hour. `gh pr view`, `gh pr list`,
`gh issue view` and `gh repo view` are all GraphQL; `gh api` reads REST core,
which has its own budget and sat at 4047/5000 through the last outage. The stub
below reproduces exactly that split.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path

SCRIPT = Path(__file__).with_name("gate.sh")

OUTAGE_CANONICAL = "example/canonical"
OUTAGE_CANONICAL_HTTPS = f"https://github.com/{OUTAGE_CANONICAL}"

# GitHub's own wording, so a reader of a failing run recognises the real thing.
OUTAGE_MESSAGE = "GraphQL: API rate limit already exceeded for user ID 1106470"

OUTAGE_GH_STUB = textwrap.dedent(
    f"""\
    #!/usr/bin/env bash
    # gh with the shared account's GraphQL budget exhausted. Every subcommand
    # other than `api` is GraphQL-backed and fails exactly as the real client
    # does; `api` answers out of the separate REST core budget.
    if [ "$1" != "api" ]; then
      echo "{OUTAGE_MESSAGE}" >&2
      exit 1
    fi
    path="$2"
    case "$path" in
      repos/*/commits/*/check-runs)
        sha="${{path#repos/{OUTAGE_CANONICAL}/commits/}}"
        sha="${{sha%/check-runs}}"
        printf '%s\\n' "$sha" >>"$OUTAGE_CHECK_RUN_LOG"
        jq -c --arg sha "$sha" '.[$sha] // {{check_runs:[]}}' <<<"$OUTAGE_CHECK_RUNS"
        ;;
      "repos/{OUTAGE_CANONICAL}/pulls/1") printf '%s\\n' "$OUTAGE_IDENTITY" ;;
      "repos/{OUTAGE_CANONICAL}/pulls/1/reviews") printf '%s\\n' "$OUTAGE_REVIEWS" ;;
      "repos/{OUTAGE_CANONICAL}/pulls/1/files") printf '1\\n' ;;
      "repos/{OUTAGE_CANONICAL}/issues/1/comments") printf '%s\\n' "$OUTAGE_COMMENTS" ;;
      repos/*/pulls\\?*) printf '%s\\n' "$OUTAGE_CLOSED_PULLS" ;;
      repos/*/rules/branches/*) printf '[]\\n' ;;
      repos/*/git/refs/heads/*) printf '%s\\n' "$OUTAGE_HEAD" ;;
      *) exit 0 ;;
    esac
    """
)

OUTAGE_GIT_SHIM = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Answers `remote get-url origin` with the canonical URL under test so the
    # preflight sees what production resolves; every other git call is real and
    # runs against the local bare repository. gate.sh carries no test seam.
    if [ "$1 $2 $3" = "remote get-url origin" ]; then
      printf '%s\\n' "$OUTAGE_ORIGIN_URL"
      exit 0
    fi
    exec "$OUTAGE_REAL_GIT" "$@"
    """
)

PASSING_CI = [{
    "name": "ci",
    "status": "completed",
    "conclusion": "success",
    "started_at": "1",
    "completed_at": "2",
}]

# A name that appears on no other head: if the baseline ever selected one of the
# pull requests it must exclude, the intersection of expected check names would
# collapse and gate.sh would print "cannot observe a common expected check name".
DECOY_CI = [{
    "name": "decoy-only",
    "status": "completed",
    "conclusion": "success",
    "started_at": "1",
    "completed_at": "2",
}]


class GateGraphqlOutageTest(unittest.TestCase):
    """#103: the whole mechanism layer stopped at gate.sh:50 when GraphQL ran out."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.bare = base / "canonical.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)

        seed = base / "seed"
        env = self.git_env()
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=env)

        def commit(message: str) -> str:
            (seed / "f.txt").write_text(message)
            subprocess.run(["git", "add", "."], cwd=seed, check=True, env=env)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=seed, check=True, env=env)
            return subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=seed, check=True, env=env,
                capture_output=True, text=True,
            ).stdout.strip()

        self.main_sha = commit("base")
        self.head_sha = commit("pr head")
        subprocess.run(
            ["git", "push", "-q", str(self.bare),
             f"{self.main_sha}:refs/heads/main", f"{self.head_sha}:refs/pull/1/head",
             f"{self.head_sha}:refs/heads/tb"],
            cwd=seed, check=True, env=env,
        )

        self.gh = base / "gh"
        self.gh.write_text(OUTAGE_GH_STUB, encoding="utf-8")
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IXUSR)

        self.check_run_log = base / "check-run-requests.log"
        self.check_run_log.write_text("", encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
        )
        return env

    def run_gate(
        self,
        *,
        closed_pulls: list[dict[str, object]] | None = None,
        check_runs_by_head: dict[str, list[dict[str, object]]] | None = None,
        body: str = "Closes #42",
        branch: str = "agent/author-issue-42",
        title: str = "Fix (#42)",
        labels: tuple[str, ...] = ("task:review", "agent:author"),
    ) -> subprocess.CompletedProcess[str]:
        base = Path(self.dir.name)
        checkout = base / "checkout"
        env = self.git_env()
        real_git = shutil.which("git")
        assert real_git is not None
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, env=env)
        subprocess.run(
            ["git", "remote", "add", "origin", self.bare.as_uri()],
            cwd=checkout, check=True, env=env,
        )
        shim = base / "git"
        if not shim.exists():
            shim.write_text(OUTAGE_GIT_SHIM, encoding="utf-8")
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        env["OUTAGE_ORIGIN_URL"] = OUTAGE_CANONICAL_HTTPS
        env["OUTAGE_REAL_GIT"] = real_git
        env["OUTAGE_HEAD"] = self.head_sha
        env["OUTAGE_CHECK_RUN_LOG"] = str(self.check_run_log)

        # The REST pull payload, exactly as GET /repos/{owner}/{repo}/pulls/{n}
        # returns it: lower-case state, `draft`, boolean `mergeable`, head.sha
        # and head.ref. gate.sh has to project this into the shape its checks
        # read; getting that projection wrong is the risk this fixture covers.
        env["OUTAGE_IDENTITY"] = json.dumps({
            "number": 1,
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": True,
            "title": title,
            "body": body,
            "head": {"sha": self.head_sha, "ref": branch, "repo": {"id": 100}},
            "base": {"repo": {"id": 100}},
            "labels": [{"name": name} for name in labels],
            "user": {"id": 1, "login": "human-author", "type": "User"},
        })
        env["OUTAGE_REVIEWS"] = json.dumps([{
            "id": 1,
            "state": "APPROVED",
            "body": f"**From:** reviewer\n\n**HEAD reviewed:** `{self.head_sha}`",
            "commit_id": self.head_sha,
            "submitted_at": "2026-01-01T00:00:00Z",
            "user": {"login": "reviewer"},
        }])
        env["OUTAGE_COMMENTS"] = json.dumps([{
            "created_at": "2026-01-01T00:00:00Z",
            "user": {"login": "reviewer"},
            "body": "looks fine to me",
        }])
        if closed_pulls is None:
            closed_pulls = [{
                "number": 9,
                "merged_at": "2026-01-09T00:00:00Z",
                "head": {"sha": self.main_sha},
            }]
        env["OUTAGE_CLOSED_PULLS"] = json.dumps(closed_pulls)
        if check_runs_by_head is None:
            check_runs_by_head = {self.head_sha: PASSING_CI, self.main_sha: PASSING_CI}
        env["OUTAGE_CHECK_RUNS"] = json.dumps({
            head: {"check_runs": runs} for head, runs in check_runs_by_head.items()
        })

        result = run_with_bash_path(
            ["bash", bash_path(SCRIPT), "1", self.head_sha],
            cwd=checkout, env=env, text=True, stub_directory=base,
            capture_output=True, check=False, timeout=120,
        )

        def remove_readonly(function, path, _exc_info):
            os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
            function(path)

        shutil.rmtree(checkout, onerror=remove_readonly)
        return result

    def logged_check_run_heads(self) -> list[str]:
        return [
            line.strip()
            for line in self.check_run_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_gate_still_reports_when_graphql_is_exhausted(self):
        # Before #103 this died at gate.sh:50 with a message about repository
        # resolution — no verdicts at all, and nothing naming the outage.
        result = self.run_gate()
        report = result.stdout + result.stderr

        self.assertNotIn("cannot resolve the repository from gh", result.stderr)
        self.assertNotEqual(result.returncode, 2, report)
        self.assertIn(f"gate: {OUTAGE_CANONICAL} #1 at {self.head_sha[:8]}", result.stdout)

        # Every dimension that REST can answer is answered.
        self.assertIn("ok      state is OPEN", result.stdout)
        self.assertIn("ok      not a draft", result.stdout)
        self.assertIn("ok      task:review is set", result.stdout)
        self.assertIn("ok      author lane is agent:author", result.stdout)
        self.assertIn("ok      body closes #42", result.stdout)
        self.assertIn("ok      1 changed file(s)", result.stdout)
        self.assertIn("ok      mergeable: MERGEABLE", result.stdout)
        self.assertIn("ok      PR head is the reviewed SHA", result.stdout)
        self.assertIn("latest run of each passing", result.stdout)

    def test_unreadable_closing_references_block_by_name(self):
        # closingIssuesReferences is GraphQL-only: REST's pull payload has no
        # such field and the issue timeline reports only cross-referenced /
        # referenced, which fire for any mention. #67 says a Development-panel
        # link closes an issue while leaving no trace in the body, so an
        # unreadable answer must never be read as "closes nothing".
        result = self.run_gate()
        self.assertIn("BLOCKED closing references unreadable", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_merged_baseline_is_the_five_most_recent_merges_over_rest(self):
        # `gh pr list --state merged` is GraphQL. REST offers no state=merged on
        # this endpoint: closed pull requests come back merged and unmerged
        # alike and merged_at is the only separator. The five heads this picks
        # define the expected check names for every future PR, so the selection
        # is asserted directly against the requests the stub observed.
        heads = {n: f"{n:x}" * 40 for n in range(3, 10)}
        expected = [heads[9], heads[8], heads[7], heads[6], heads[5]]
        # Deliberately not in merged order, and with the unmerged pull request
        # first: creation order is what GitHub returns, merged_at is what counts.
        closed_pulls = [
            {"number": 3, "merged_at": None, "head": {"sha": heads[3]}},
            {"number": 5, "merged_at": "2026-01-05T00:00:00Z", "head": {"sha": heads[5]}},
            {"number": 9, "merged_at": "2026-01-09T00:00:00Z", "head": {"sha": heads[9]}},
            {"number": 4, "merged_at": "2026-01-04T00:00:00Z", "head": {"sha": heads[4]}},
            {"number": 7, "merged_at": "2026-01-07T00:00:00Z", "head": {"sha": heads[7]}},
            {"number": 6, "merged_at": "2026-01-06T00:00:00Z", "head": {"sha": heads[6]}},
            {"number": 8, "merged_at": "2026-01-08T00:00:00Z", "head": {"sha": heads[8]}},
        ]
        check_runs_by_head = {self.head_sha: PASSING_CI}
        for sha in expected:
            check_runs_by_head[sha] = PASSING_CI
        # The two that must be excluded carry a name nothing else has.
        check_runs_by_head[heads[4]] = DECOY_CI
        check_runs_by_head[heads[3]] = DECOY_CI

        result = self.run_gate(
            closed_pulls=closed_pulls, check_runs_by_head=check_runs_by_head
        )
        observed = self.logged_check_run_heads()
        self.assertEqual(observed[:1], [self.head_sha], result.stdout + result.stderr)
        self.assertEqual(observed[1:], expected, result.stdout + result.stderr)
        self.assertNotIn("cannot observe a common expected check name", result.stdout)
        self.assertNotIn("bootstrapping from checks observed", result.stdout)
        self.assertIn("latest run of each passing", result.stdout)

    def test_the_comment_tail_survives_the_outage(self):
        # `gh pr view --json comments` is GraphQL and returns the same set the
        # REST issue-comments read already fetched for check 5d.
        result = self.run_gate()
        self.assertIn("--- last 3 comments ---", result.stdout)
        self.assertIn("2026-01-01T00:00:00Z reviewer: looks fine to me", result.stdout)


if __name__ == "__main__":
    unittest.main()
