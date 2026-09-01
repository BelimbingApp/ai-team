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

CANONICAL_HTTPS = "https://github.com/example/canonical"
CANONICAL_SSH = "git@github.com:example/canonical"

GH_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Stubbed gh for gate.sh mechanism tests. Values arrive via GATE_TEST_* env.
    case "$1 $2" in
      "repo view") printf '%s\\n' "$GATE_TEST_CANONICAL" ;;
      "pr view")
        body="${GATE_TEST_BODY:-Closes #42}"
        branch="${GATE_TEST_BRANCH:-agent/author-issue-42}"
        title="${GATE_TEST_TITLE:-Fix (#42)}"
        jq -n \
          --arg head "$GATE_TEST_HEAD" \
          --arg branch "$branch" \
          --arg title "$title" \
          --arg body "$body" \
          --argjson labels "$GATE_TEST_LABELS" \
          '{headRefOid:$head,headRefName:$branch,title:$title,body:$body,isDraft:false,state:"OPEN",mergeable:"MERGEABLE",labels:$labels}'
        ;;
      "pr list")
        printf '%s\\n' "$GATE_TEST_MERGED_HEADS"
        ;;
      "api repos/$GATE_TEST_CANONICAL/commits/"*check-runs*)
        head="${2#repos/$GATE_TEST_CANONICAL/commits/}"
        head="${head%/check-runs}"
        if [ "$head" = "$GATE_TEST_HEAD" ]; then
          printf '%s\\n' "$GATE_TEST_CHECK_RUNS"
        elif [ "$head" = "$GATE_TEST_MAIN_SHA" ]; then
          printf '%s\\n' "$GATE_TEST_MAIN_CHECK_RUNS"
        elif [ -n "${GATE_TEST_BASELINE_CHECK_RUNS:-}" ]; then
          jq -c --arg head "$head" '.[$head] // {check_runs:[]}' <<<"$GATE_TEST_BASELINE_CHECK_RUNS"
        elif [ -n "${GATE_TEST_CHECK_RUNS:-}" ]; then
          printf '%s\\n' "$GATE_TEST_CHECK_RUNS"
        else
          printf '{"check_runs":[{"name":"ci","status":"completed","conclusion":"success","started_at":"1","completed_at":"2"}]}\\n'
        fi
        ;;
      "api repos/$GATE_TEST_CANONICAL/commits/"*)
        [ -n "$GATE_TEST_RESOLVE" ] && printf '%s\\n' "$GATE_TEST_RESOLVE"
        ;;
      "api repos/$GATE_TEST_CANONICAL/rules/branches/"*)
        printf '%s\\n' "$GATE_TEST_BRANCH_RULES"
        ;;
      "api repos/$GATE_TEST_CANONICAL/git/refs/heads/"*)
        printf '%s\\n' "$GATE_TEST_HEAD"
        ;;
      "api repos/$GATE_TEST_CANONICAL/pulls/1/reviews")
        printf '%s\\n' "$GATE_TEST_REVIEWS"
        ;;
      "api repos/$GATE_TEST_CANONICAL/issues/1/comments")
        printf '%s\\n' "${GATE_TEST_ISSUE_COMMENTS:-[]}"
        ;;
      "api repos/$GATE_TEST_CANONICAL/pulls/1/files")
        printf '1\\n'
        ;;
      *) exit 0 ;;
    esac
    """
)

GIT_SHIM = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Answers `remote get-url origin` with the URL under test so the preflight
    # sees exactly what production would resolve; everything else runs real
    # git against the local bare remote. gate.sh itself carries no test seam.
    if [ "$1 $2 $3" = "remote get-url origin" ]; then
      printf '%s\\n' "$GATE_TEST_ORIGIN_URL"
      exit 0
    fi
    exec "$GATE_TEST_REAL_GIT" "$@"
    """
)


class GateMechanismTest(unittest.TestCase):
    """Hermetic regressions for gate.sh: fork-origin/canonical split, reviewed-SHA
    resolution, and the full PASS/refuse verdicts. No network: the canonical URL
    is rewritten to a local bare repository via git insteadOf, and gh is a stub."""

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
        self.gh.write_text(GH_STUB, encoding="utf-8")
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IXUSR)

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
        origin: str,
        reviewed: str | None,
        resolve: str = "",
        head: str | None = None,
        labels: list[str] | None = None,
        reviews: list[dict[str, object]] | None = None,
        issue_comments: list[dict[str, object]] | None = None,
        check_runs: list[dict[str, object]] | None = None,
        main_check_runs: list[dict[str, object]] | None = None,
        merged_head: str | None = None,
        baseline_check_runs: list[dict[str, object]] | None = None,
        merged_heads: list[str] | None = None,
        baseline_check_runs_by_head: dict[str, list[dict[str, object]]] | None = None,
        body: str | None = None,
        branch: str | None = None,
        title: str | None = None,
        review_gate_body: str | None = None,
        branch_rules: list[dict[str, object]] | None = None,
        allow_missing_checks: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        base = Path(self.dir.name)
        checkout = base / "checkout"
        env = self.git_env()
        real_git = shutil.which("git")
        assert real_git is not None
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, env=env)
        # Fetches go straight to the local bare repo — hermetic by construction.
        # The git shim (on PATH below) answers only `remote get-url origin` with
        # the URL under test, so the preflight exercises the same
        # resolved-transport check production runs, without any network.
        subprocess.run(
            ["git", "remote", "add", "origin", self.bare.as_uri()],
            cwd=checkout, check=True, env=env,
        )
        shim = base / "git"
        if not shim.exists():
            shim.write_text(GIT_SHIM, encoding="utf-8")
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        env["GATE_TEST_ORIGIN_URL"] = origin
        env["GATE_TEST_REAL_GIT"] = real_git

        env["GATE_TEST_CANONICAL"] = "example/canonical"
        env["GATE_TEST_MAIN_SHA"] = self.main_sha
        effective_head = head or self.head_sha
        env["GATE_TEST_HEAD"] = effective_head
        env["GATE_TEST_RESOLVE"] = resolve
        if body is not None:
            env["GATE_TEST_BODY"] = body
        if branch is not None:
            env["GATE_TEST_BRANCH"] = branch
        if title is not None:
            env["GATE_TEST_TITLE"] = title
        effective_labels = labels if labels is not None else ["task:review", "agent:author"]
        env["GATE_TEST_LABELS"] = json.dumps([
            {"name": label} for label in effective_labels
        ])
        if reviews is None:
            reviews = [{
                "id": 1,
                "state": "APPROVED",
                "body": "**From:** reviewer",
                "commit_id": effective_head,
                "submitted_at": "2026-01-01T00:00:00Z",
                "user": {"login": "reviewer"},
            }]
        env["GATE_TEST_REVIEWS"] = json.dumps(reviews)
        env["GATE_TEST_ISSUE_COMMENTS"] = json.dumps(issue_comments if issue_comments is not None else [])
        if check_runs is None:
            check_runs = [{
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            }]
        env["GATE_TEST_CHECK_RUNS"] = json.dumps({"check_runs": check_runs})
        if main_check_runs is None:
            main_check_runs = [{
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            }]
        if baseline_check_runs is None:
            baseline_check_runs = main_check_runs
        env["GATE_TEST_MAIN_CHECK_RUNS"] = json.dumps({"check_runs": main_check_runs})
        if merged_heads is None:
            merged_heads = [self.main_sha] if merged_head is None else ([merged_head] if merged_head else [])
        if baseline_check_runs_by_head is None:
            baseline = main_check_runs if baseline_check_runs is None else baseline_check_runs
            baseline_check_runs_by_head = {head: baseline for head in merged_heads}
        env["GATE_TEST_MERGED_HEADS"] = "\n".join(merged_heads)
        env["GATE_TEST_BASELINE_CHECK_RUNS"] = json.dumps({
            head: {"check_runs": runs} for head, runs in baseline_check_runs_by_head.items()
        })
        env["GATE_TEST_BRANCH_RULES"] = json.dumps(
            [] if branch_rules is None else branch_rules
        )
        if allow_missing_checks is not None:
            env["GATE_ALLOW_MISSING_CHECKS"] = allow_missing_checks

        script = SCRIPT
        if review_gate_body is not None:
            mechanisms = base / "mechanisms"
            mechanisms.mkdir()
            for name in ("gate.sh", "review_gate.sh", "_lane_issue.sh", "_default_branch.sh"):
                shutil.copy2(SCRIPT.with_name(name), mechanisms / name)
            review_gate = mechanisms / "review_gate.sh"
            review_gate.write_text(review_gate_body, encoding="utf-8")
            review_gate.chmod(review_gate.stat().st_mode | stat.S_IXUSR)
            script = mechanisms / "gate.sh"

        args = ["bash", bash_path(script), "1"]
        if reviewed is not None:
            args.append(reviewed)

        result = run_with_bash_path(
            args, cwd=checkout, env=env, text=True,
            stub_directory=base,
            capture_output=True, check=False, timeout=60,
        )
        def remove_readonly(function, path, _exc_info):
            os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
            function(path)

        shutil.rmtree(checkout, onerror=remove_readonly)
        return result

    def test_rewritten_transport_refused_despite_canonical_label(self):
        # The insteadOf threat: the configured URL can look canonical while an
        # url.*.insteadOf rule redirects the actual fetch. Production reads the
        # *resolved* transport, so what matters is what get-url returns — here
        # the local bare path — and the gate must refuse it pre-verdict.
        result = self.run_gate(origin=self.bare.as_uri(), reviewed=self.head_sha)
        self.assertEqual(result.returncode, 2)
        self.assertIn("origin must be the", result.stderr)
        self.assertNotIn("gate:", result.stdout)

    def test_fork_origin_fails_before_any_verdict(self):
        result = self.run_gate(origin="https://github.com/example/fork", reviewed=self.head_sha)
        self.assertEqual(result.returncode, 2)
        self.assertIn("origin must be the", result.stderr)
        self.assertNotIn("gate:", result.stdout)

    def test_full_sha_on_canonical_origin_passes_completely(self):
        for origin in (CANONICAL_HTTPS, CANONICAL_SSH):
            result = self.run_gate(origin=origin, reviewed=self.head_sha)
            self.assertEqual(result.returncode, 0, (origin, result.stdout, result.stderr))
            self.assertIn("GATE: PASS", result.stdout)
            self.assertIn("PR head is the reviewed SHA", result.stdout)

    def test_missing_native_approval_warns_without_reclassifying_ai_team_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            branch_rules=[{
                "type": "pull_request",
                "parameters": {"required_approving_review_count": 1},
            }],
            reviews=[{
                "id": 1,
                "state": "COMMENTED",
                "body": "**From:** reviewer\n\n**Verdict:** accept",
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
                "user": {"login": "reviewer"},
            }],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN", result.stdout)
        self.assertIn("GitHub requires 1 native approval(s)", result.stdout)
        self.assertIn("only 0 distinct current APPROVED reviewer(s) are visible", result.stdout)
        self.assertIn("separate eligible native reviewer or automation", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_present_native_approval_is_reported_without_a_warning(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            branch_rules=[{
                "type": "pull_request",
                "parameters": {"required_approving_review_count": 1},
            }],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "GitHub native approval preflight: requires 1, 1 distinct current APPROVED reviewer(s) visible",
            result.stdout,
        )
        self.assertIn("GitHub still decides eligibility and freshness", result.stdout)
        self.assertNotIn("only 0 distinct current APPROVED reviewer(s) are visible", result.stdout)

    def test_duplicate_approvals_from_one_reviewer_do_not_satisfy_the_count(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            branch_rules=[{
                "type": "pull_request",
                "parameters": {"required_approving_review_count": 2},
            }],
            reviews=[
                {
                    "id": 1,
                    "state": "APPROVED",
                    "body": "**From:** reviewer\n\n**Verdict:** accept",
                    "commit_id": self.head_sha,
                    "submitted_at": "2026-01-01T00:00:00Z",
                    "user": {"login": "reviewer"},
                },
                {
                    "id": 2,
                    "state": "APPROVED",
                    "body": "**From:** reviewer\n\n**Verdict:** accept",
                    "commit_id": self.head_sha,
                    "submitted_at": "2026-01-02T00:00:00Z",
                    "user": {"login": "reviewer"},
                },
            ],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("requires 2 native approval(s)", result.stdout)
        self.assertIn("only 1 distinct current APPROVED reviewer(s) are visible", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_later_nonapproval_supersedes_an_earlier_approval_for_the_preflight(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            branch_rules=[{
                "type": "pull_request",
                "parameters": {"required_approving_review_count": 1},
            }],
            reviews=[
                {
                    "id": 1,
                    "state": "APPROVED",
                    "body": "**From:** reviewer\n\n**Verdict:** accept",
                    "commit_id": self.head_sha,
                    "submitted_at": "2026-01-01T00:00:00Z",
                    "user": {"login": "reviewer"},
                },
                {
                    "id": 2,
                    "state": "CHANGES_REQUESTED",
                    "body": "**From:** reviewer\n\n**Verdict:** changes required",
                    "commit_id": self.head_sha,
                    "submitted_at": "2026-01-02T00:00:00Z",
                    "user": {"login": "reviewer"},
                },
            ],
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("requires 1 native approval(s)", result.stdout)
        self.assertIn("only 0 distinct current APPROVED reviewer(s) are visible", result.stdout)

    def test_latest_success_supersedes_a_cancelled_run_with_the_same_name(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "cancelled",
                    "started_at": "1",
                    "completed_at": "2",
                },
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "3",
                    "completed_at": "4",
                },
            ],
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("1 distinct checks", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_failing_check_run_blocks_the_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[{
                "name": "ci",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "1",
                "completed_at": "2",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 distinct, 1 not passing", result.stdout)
        self.assertIn("ci: completed/failure", result.stdout)

    def test_pending_check_run_blocks_the_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[{
                "name": "ci",
                "status": "in_progress",
                "conclusion": None,
                "started_at": "1",
                "completed_at": None,
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 distinct, 1 not passing", result.stdout)
        self.assertIn("ci: in_progress/pending", result.stdout)

    def test_no_reported_check_runs_blocks_with_the_actual_condition(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no checks reported yet", result.stdout)
        self.assertNotIn("need >=", result.stdout)

    def test_missing_expected_check_name_blocks_before_it_reports(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[{
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            }],
            main_check_runs=[
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "1",
                    "completed_at": "2",
                },
                {
                    "name": "quality",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "1",
                    "completed_at": "2",
                },
            ],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("checks not yet reported", result.stdout)
        self.assertIn("quality", result.stdout)

    def test_default_branch_only_check_is_not_expected_for_a_pull_request(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[{
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            }],
            main_check_runs=[
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "1",
                    "completed_at": "2",
                },
                {
                    "name": "nightly sweep",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "1",
                    "completed_at": "2",
                },
            ],
            merged_head="merged-pr-head",
            baseline_check_runs=[{
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            }],
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("1 distinct checks", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)
        self.assertNotIn("nightly sweep", result.stdout)

    def test_expected_check_names_are_observed_from_merged_pr_not_encoded_as_a_count(self):
        runs = [
            {
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            },
            {
                "name": "quality",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            },
        ]
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=runs,
            main_check_runs=runs,
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("2 distinct checks", result.stdout)

    def test_expected_check_names_intersect_recent_merged_pr_heads(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[{
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            }],
            merged_heads=["merged-pr-head-1", "merged-pr-head-2"],
            baseline_check_runs_by_head={
                "merged-pr-head-1": [
                    {"name": "ci", "status": "completed", "conclusion": "success"},
                    {"name": "path-filtered", "status": "completed", "conclusion": "success"},
                ],
                "merged-pr-head-2": [
                    {"name": "ci", "status": "completed", "conclusion": "success"},
                ],
            },
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("1 distinct checks", result.stdout)
        self.assertNotIn("path-filtered", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_operator_override_allows_intentionally_removed_checks(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[{
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "started_at": "1",
                "completed_at": "2",
            }],
            merged_heads=["merged-pr-head-1"],
            baseline_check_runs_by_head={"merged-pr-head-1": [
                {"name": "ci", "status": "completed", "conclusion": "success"},
                {"name": "removed workflow", "status": "completed", "conclusion": "success"},
            ]},
            allow_missing_checks="removed workflow",
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("operator override allows missing checks: removed workflow", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_first_pull_request_bootstraps_from_passing_reviewed_sha(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            main_check_runs=[],
            merged_head="",
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("no merged pull request baseline is available", result.stdout)
        self.assertIn("bootstrap", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_first_pull_request_without_checks_still_blocks(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[],
            main_check_runs=[],
            merged_head="",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no checks reported yet", result.stdout)

    def test_first_pull_request_with_a_failing_check_still_blocks(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[{
                "name": "ci",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "1",
                "completed_at": "2",
            }],
            main_check_runs=[],
            merged_heads=[],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 distinct, 1 not passing", result.stdout)
        self.assertIn("ci: completed/failure", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_first_pull_request_with_a_pending_check_still_blocks(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            check_runs=[{
                "name": "ci",
                "status": "in_progress",
                "conclusion": None,
                "started_at": "1",
                "completed_at": None,
            }],
            main_check_runs=[],
            merged_heads=[],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 distinct, 1 not passing", result.stdout)
        self.assertIn("ci: in_progress/pending", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_observed_merged_pr_without_checks_does_not_bootstrap(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            main_check_runs=[],
            merged_head="merged-pr-head",
            baseline_check_runs=[],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot observe a common expected check name", result.stdout)

    def test_missing_independent_review_fails_the_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS, reviewed=self.head_sha, reviews=[]
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_silent_review_delegate_cannot_erase_the_review_dimension(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            review_gate_body="#!/usr/bin/env bash\nexit 0\n",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "review gate did not report an independent exact-head acceptance",
            result.stdout,
        )
        self.assertIn("GATE: FAIL", result.stdout)

    def test_same_lane_approval_is_not_independent(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "APPROVED",
                "body": "**From:** author",
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_stale_approval_does_not_cover_the_current_head(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "APPROVED",
                "body": "**From:** reviewer",
                "commit_id": self.main_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_shared_account_comment_with_explicit_acceptance_passes(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "COMMENTED",
                "body": "**From:** reviewer\n\n**Verdict:** accept",
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_literal_backslash_n_does_not_create_marker_lines(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "COMMENTED",
                "body": r"**From:** reviewer\n\n**Verdict:** accept",
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_stray_comment_verdict_warns_and_still_fails_the_gate(self):
        # gh pr review --approve is refused on the shared account; the natural
        # fallback gh pr comment posts fine but gate.sh reads pulls/:pr/reviews
        # only. #359: this must be a loud, named WARN, not indistinguishable
        # from "nobody reviewed".
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[],
            issue_comments=[{
                "id": 1,
                "body": "**From:** reviewer\n\n**Verdict:** accept",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("WARN", result.stdout)
        self.assertIn("found a verdict marker from reviewer in the comment stream", result.stdout)
        self.assertIn("gh pr review --comment", result.stdout)

    def test_inline_verdict_in_the_comment_stream_still_warns(self):
        # The observed #356 incident: an agent improvising the channel (gh pr
        # comment, after --approve was refused) improvised the formatting too
        # — "**From:** opus-5 — **Verdict:** accept at `sha`." on one line.
        # The comment-stream scan is diagnostic only (never grants an
        # acceptance), so unlike 5c it must not require line-anchoring, or
        # exactly this case goes undetected.
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[],
            issue_comments=[{
                "id": 1,
                "body": "**From:** reviewer — **Verdict:** accept at `abc1234`.",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("found a verdict marker from reviewer in the comment stream", result.stdout)

    def test_blocking_comment_verdict_warns_even_when_a_real_acceptance_exists(self):
        # Comment-stream markers never become verdicts, but a real acceptance
        # must not hide another reviewer's explicit blocking marker.
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "APPROVED",
                "body": "**From:** reviewer",
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
            issue_comments=[{
                "id": 1,
                "body": "**From:** someone-else\n\n**Verdict:** changes required",
            }],
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("WARN", result.stdout)
        self.assertIn("blocking verdict marker from someone-else", result.stdout)
        self.assertIn("gh pr review --comment", result.stdout)

    def test_unfetchable_reviewed_sha_is_not_misreported_as_behind(self):
        missing_sha = "f" * 40
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=missing_sha,
            head=missing_sha,
            reviews=[{
                "id": 1,
                "state": "APPROVED",
                "body": "**From:** reviewer",
                "commit_id": missing_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("is unavailable after fetching PR #1", result.stdout)
        self.assertIn("history may have been rewritten", result.stdout)
        self.assertNotIn("BEHIND origin/main", result.stdout)

    def test_malformed_review_marker_warns_about_format_instead_of_silence(self):
        # A **From:** marker is present, but the verdict is inline rather than
        # on its own line — gate.sh must say the marker was seen and rejected
        # for format, not just omit the reviewer as if they never posted.
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "COMMENTED",
                "body": "**From:** reviewer — **Verdict:** accept",
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("WARN", result.stdout)
        self.assertIn("a review marker from reviewer was seen", result.stdout)
        self.assertIn("rejected for format", result.stdout)
        self.assertIn("own line", result.stdout)

    def test_ambiguous_reviewer_identity_does_not_create_acceptance(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "COMMENTED",
                "body": (
                    "**From:** author\n"
                    "**From:** reviewer\n\n"
                    "**Verdict:** accept"
                ),
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_repeated_identical_reviewer_marker_is_unambiguous(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "COMMENTED",
                "body": (
                    "**From:** reviewer\n"
                    "**From:** reviewer\n\n"
                    "**Verdict:** accept"
                ),
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)

    def test_conflicting_verdict_markers_do_not_create_acceptance(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "COMMENTED",
                "body": (
                    "**From:** reviewer\n\n"
                    "**Verdict:** accept\n"
                    "**Verdict:** changes required"
                ),
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_native_approval_cannot_override_conflicting_verdict_markers(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "APPROVED",
                "body": (
                    "**From:** reviewer\n\n"
                    "**Verdict:** accept\n"
                    "**Verdict:** changes required"
                ),
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_native_changes_requested_survives_conflicting_verdict_markers(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "CHANGES_REQUESTED",
                "body": (
                    "**From:** reviewer\n\n"
                    "**Verdict:** accept\n"
                    "**Verdict:** changes required"
                ),
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("independent exact-head changes required by reviewer", result.stdout)

    def test_latest_changes_required_verdict_blocks_an_earlier_acceptance(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[
                {
                    "id": 1,
                    "state": "COMMENTED",
                    "body": "**From:** reviewer\n\n**Verdict:** accept",
                    "commit_id": self.head_sha,
                    "submitted_at": "2026-01-01T00:00:00Z",
                },
                {
                    "id": 2,
                    "state": "COMMENTED",
                    "body": "**From:** reviewer\n\n**Verdict:** changes required",
                    "commit_id": self.head_sha,
                    "submitted_at": "2026-01-01T00:01:00Z",
                },
            ],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("independent exact-head changes required by reviewer", result.stdout)

    def test_latest_ambiguous_verdict_revokes_an_earlier_acceptance(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[
                {
                    "id": 1,
                    "state": "COMMENTED",
                    "body": "**From:** reviewer\n\n**Verdict:** accept",
                    "commit_id": self.head_sha,
                    "submitted_at": "2026-01-01T00:00:00Z",
                },
                {
                    "id": 2,
                    "state": "COMMENTED",
                    "body": (
                        "**From:** reviewer\n\n"
                        "**Verdict:** accept\n"
                        "**Verdict:** changes required"
                    ),
                    "commit_id": self.head_sha,
                    "submitted_at": "2026-01-01T00:01:00Z",
                },
            ],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("no independent exact-head changes-required verdict", result.stdout)

    def test_dismissed_review_cannot_authorize_the_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            reviews=[{
                "id": 1,
                "state": "DISMISSED",
                "body": "**From:** reviewer\n\n**Verdict:** accept",
                "commit_id": self.head_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_task_active_is_not_a_ready_handoff(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            labels=["task:active", "agent:author"],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("task:review is not set", result.stdout)

    def test_missing_or_multiple_author_lanes_fail_the_gate(self):
        for labels in (
            ["task:review"],
            ["task:review", "agent:author", "agent:second-author"],
        ):
            with self.subTest(labels=labels):
                result = self.run_gate(
                    origin=CANONICAL_HTTPS,
                    reviewed=self.head_sha,
                    labels=labels,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("expected exactly one agent:<id> author lane", result.stdout)

    def test_short_abbreviation_refused(self):
        result = self.run_gate(origin=CANONICAL_HTTPS, reviewed=self.head_sha[:8])
        self.assertEqual(result.returncode, 2)
        self.assertIn("too short", result.stderr)
        self.assertNotIn("GATE:", result.stdout)

    def test_unresolved_abbreviation_refused(self):
        result = self.run_gate(origin=CANONICAL_HTTPS, reviewed=self.head_sha[:12], resolve="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not resolve", result.stderr)
        self.assertNotIn("GATE:", result.stdout)

    def test_resolved_abbreviation_passes_completely(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS, reviewed=self.head_sha[:12], resolve=self.head_sha
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn(f"resolved abbreviated {self.head_sha[:12]} to {self.head_sha}", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_moved_head_fails_the_gate(self):
        # Reviewed the older commit; the PR head has moved on.
        result = self.run_gate(
            origin=CANONICAL_HTTPS, reviewed=self.main_sha[:12], resolve=self.main_sha
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("re-review the new head", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_missing_closes_keyword_fails_the_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            body="**From:** author\n\nImplementation notes only.\n",
            title="Deployment gate (#42)",
            branch="agent/author-issue-42",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no closing reference to #42", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_closes_keyword_for_lane_issue_passes(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            body="**From:** author\n\nCloses #42\n",
            title="Deployment gate (#42)",
            branch="agent/author-issue-42",
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("body closes #42", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_conflicting_title_and_branch_fails_the_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            body="Closes #99\n",
            title="Backport context (#99) for lane (#42)",
            branch="agent/author-issue-42",
        )
        # Trailing title (#42) agrees with branch — passes identity; body must close #42.
        self.assertEqual(result.returncode, 1)
        self.assertIn("no closing reference to #42", result.stdout)

    def test_title_branch_number_conflict_fails_the_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            body="Closes #999\n",
            title="renamed lane (#999)",
            branch="agent/author-issue-42",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("disagrees", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_issue_less_lane_with_marker_passes(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            body="AI-Team-Lane-Issue: none\n\nNo tracker issue.\n",
            title="Ad-hoc mechanism tweak",
            branch="agent/author-misc",
        )
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("issue-less lane", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_underivable_lane_without_marker_fails_the_gate(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            body="Closes #99\n",
            title="Ad-hoc change",
            branch="agent/author-misc",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot derive issue", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)


    # -- #385: hold:review:<agent> — every named holder blocks independently,
    # clearing one never clears another, and a legacy bare hold:review still
    # blocks unattributed during migration. --

    def test_no_hold_labels_pass_both_hold_checks(self):
        result = self.run_gate(origin=CANONICAL_HTTPS, reviewed=self.head_sha)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("no hold:author", result.stdout)
        self.assertIn("no named hold:review:<agent>", result.stdout)
        self.assertIn("no unattributed hold:review", result.stdout)
        self.assertIn("GATE: PASS", result.stdout)

    def test_a_single_named_hold_blocks_and_names_its_holder(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            labels=["task:review", "agent:author", "hold:review:sol"],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("hold:review held by sol", result.stdout)
        self.assertIn("hold.sh review clear", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_two_named_holders_are_both_reported(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            labels=["task:review", "agent:author", "hold:review:sol", "hold:review:luna"],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("hold:review held by", result.stdout)
        self.assertIn("sol", result.stdout)
        self.assertIn("luna", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_clearing_one_holders_label_leaves_the_others_finding_blocking(self):
        # The exact regression #385 was filed for: two independent holders,
        # one clears, the other's finding must still block the merge.
        both = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            labels=["task:review", "agent:author", "hold:review:sol", "hold:review:luna"],
        )
        self.assertEqual(both.returncode, 1)

        # sol's hold cleared (label removed); luna's remains.
        after_sol_clears = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            labels=["task:review", "agent:author", "hold:review:luna"],
        )
        self.assertEqual(after_sol_clears.returncode, 1)
        self.assertIn("hold:review held by luna", after_sol_clears.stdout)
        self.assertNotIn("sol", after_sol_clears.stdout)
        self.assertIn("GATE: FAIL", after_sol_clears.stdout)

        # Both cleared: the gate's hold checks pass (other checks still apply).
        after_both_clear = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            labels=["task:review", "agent:author"],
        )
        self.assertEqual(after_both_clear.returncode, 0, (after_both_clear.stdout, after_both_clear.stderr))
        self.assertIn("no named hold:review:<agent>", after_both_clear.stdout)

    def test_legacy_unattributed_hold_review_still_blocks_during_migration(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            labels=["task:review", "agent:author", "hold:review"],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("hold:review (unattributed, pre-#385) is set", result.stdout)
        self.assertIn("GATE: FAIL", result.stdout)

    def test_hold_author_is_unaffected_by_the_named_review_hold_change(self):
        result = self.run_gate(
            origin=CANONICAL_HTTPS,
            reviewed=self.head_sha,
            labels=["task:review", "agent:author", "hold:author"],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("hold:author is set", result.stdout)


if __name__ == "__main__":
    unittest.main()
