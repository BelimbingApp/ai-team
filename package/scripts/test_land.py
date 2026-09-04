import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


SCRIPT = Path(__file__).with_name("land.sh")
LANE = Path(__file__).with_name("_lane_issue.sh")
DEFAULT_BRANCH = Path(__file__).with_name("_default_branch.sh")
TRUSTED_AUTHOR = Path(__file__).with_name("_trusted_author.sh")
HYGIENE = Path(__file__).with_name("label_hygiene.sh")
CANONICAL_UNPROTECTED_JSON = (
    '{"message":"Branch not protected",'
    '"documentation_url":"https://docs.github.com/rest/branches/'
    'branch-protection#get-branch-protection","status":"404"}'
)
# Captured from the production command with xxd: the API body has no newline,
# so gh's stderr diagnostic begins immediately after the closing brace.
CANONICAL_UNPROTECTED_RESPONSE = (
    CANONICAL_UNPROTECTED_JSON + "gh: Branch not protected (HTTP 404)"
)
REORDERED_UNPROTECTED_JSON = (
    '{"status":"404","message":"Branch not protected",'
    '"documentation_url":"https://docs.github.com/rest/branches/'
    'branch-protection#get-branch-protection"}'
)
DUPLICATE_UNPROTECTED_RESPONSES = [
    (
        '{"message":"Not Found","message":"Branch not protected",'
        '"documentation_url":"https://docs.github.com/rest/branches/'
        'branch-protection#get-branch-protection","status":"404"}'
    ),
    (
        '{"message":"Branch not protected",'
        '"documentation_url":"https://example.invalid/concealed",'
        '"documentation_url":"https://docs.github.com/rest/branches/'
        'branch-protection#get-branch-protection","status":"404"}'
    ),
    (
        '{"message":"Branch not protected",'
        '"documentation_url":"https://docs.github.com/rest/branches/'
        'branch-protection#get-branch-protection",'
        '"status":"403","status":"404"}'
    ),
]


class LandHarness(unittest.TestCase):
    """Stubbed gh/gate fixture shared by every land test class."""

    """Hermetic regressions for the gate-to-terminal lane transition."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.scripts = base / "scripts"
        self.scripts.mkdir()
        for path in (SCRIPT, LANE, DEFAULT_BRANCH, TRUSTED_AUTHOR):
            destination = self.scripts / path.name
            destination.write_bytes(path.read_bytes())
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)

        self.gate_log = base / "gate.log"
        gate = self.scripts / "gate.sh"
        gate.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                printf '%s\\n' "$*" >>"$LAND_TEST_GATE_LOG"
                exit "${LAND_TEST_GATE_STATUS:-0}"
                """
            ),
            encoding="utf-8",
        )
        gate.chmod(gate.stat().st_mode | stat.S_IXUSR)

        self.gh_log = base / "gh.log"
        gh = base / "bin" / "gh"
        gh.parent.mkdir()
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\\n' "$*" >>"$LAND_TEST_GH_LOG"
                case "$1 $2" in
                  "repo view")
                    printf 'example/canonical\\n'
                    ;;
                  "pr view")
                    jq -n \\
                      --arg state "${LAND_TEST_STATE:-OPEN}" \\
                      --arg sha "${LAND_TEST_MERGE_SHA:-}" \\
                      --arg attr "${LAND_TEST_ATTRIBUTION:-}" \\
                      --arg title "$LAND_TEST_TITLE" \\
                      --arg body "$LAND_TEST_BODY" \\
                      --arg branch "$LAND_TEST_BRANCH" \\
                      --arg base "$LAND_TEST_BASE_BRANCH" \\
                      --argjson labels "$LAND_TEST_LABELS" \\
                      '{number:42,title:$title,body:$body,headRefName:$branch,baseRefName:$base,labels:$labels,isDraft:false,state:$state,mergeCommit:(if $state == "MERGED" then {oid:$sha} else null end),comments:(if $attr == "" then [] else [{body:$attr}] end)}'
                    ;;
                  "api repos/example/canonical/pulls/42")
                    printf '%s\\n' "$LAND_TEST_IDENTITY"
                    ;;
                  "api repos/example/canonical")
                    if [ "${LAND_TEST_SETTINGS_STATUS:-0}" != "0" ]; then
                      printf 'gh: could not read repository\\n' >&2
                      exit "${LAND_TEST_SETTINGS_STATUS}"
                    fi
                    jq -n \\
                      --argjson merge "${LAND_TEST_ALLOW_MERGE:-true}" \\
                      --argjson squash "${LAND_TEST_ALLOW_SQUASH:-true}" \\
                      --argjson rebase "${LAND_TEST_ALLOW_REBASE:-true}" \\
                      '{allow_merge_commit:$merge,allow_squash_merge:$squash,allow_rebase_merge:$rebase}'
                    ;;
                  "api repos/example/canonical/branches/main/protection")
                    if [ "${LAND_TEST_PROTECTION_STATUS:-404}" != "0" ]; then
                      printf '%s\\n' "$LAND_TEST_PROTECTION_FAILURE" >&2
                      exit 1
                    fi
                    printf '%s\\n' "$LAND_TEST_PROTECTION"
                    ;;
                  "api -X")
                    if [ "${3:-}" = "PUT" ]; then
                      if [ "${LAND_TEST_MERGE_REQUEST_STATUS:-0}" != "0" ]; then
                        printf '%s\\n' "${LAND_TEST_MERGE_FAILURE:-gh: merge endpoint rejected the request (HTTP 405)}" >&2
                        exit "${LAND_TEST_MERGE_REQUEST_STATUS}"
                      fi
                      if [ -n "${LAND_TEST_MERGE_MESSAGE:-}" ]; then
                        jq -n --arg message "$LAND_TEST_MERGE_MESSAGE" '{merged:false,message:$message}'
                        exit 0
                      fi
                      if [[ " $* " != *" -f sha=${LAND_TEST_REVIEWED:-} "* ]]; then
                        printf '{"merged":false,"message":"head changed"}\\n'
                        exit 0
                      fi
                      printf '{"merged":true,"sha":"%s"}\\n' "${LAND_TEST_MERGE_SHA:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
                    fi
                    ;;
                  "api --paginate")
                    if [[ "$*" != *"rules/branches/main?per_page=100"* ]]; then
                      echo "unexpected paginated gh: $*" >&2
                      exit 1
                    fi
                    if [ "${LAND_TEST_RULES_STATUS:-0}" != "0" ]; then
                      printf 'gh: could not read active rules\\n' >&2
                      exit "${LAND_TEST_RULES_STATUS}"
                    fi
                    printf '%s\\n' "$LAND_TEST_RULES_PAGES"
                    ;;
                  "pr list")
                    printf '%s\\n' "${LAND_TEST_STACKED:-}"
                    ;;
                  "pr comment"|"pr edit"|"issue edit")
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
        self.cwd = base

    def tearDown(self):
        self.dir.cleanup()

    def run_land(
        self,
        *,
        gate_status: str = "0",
        state: str = "OPEN",
        attributed: bool = False,
        merge_request_status: str = "0",
        merge_failure: str = "",
        merge_message: str = "",
        trusted_bot: bool = False,
        reviewed: str = "a" * 40,
        stacked: str = "",
        allow_merge: str = "true",
        allow_squash: str = "true",
        allow_rebase: str = "true",
        settings_status: str = "0",
        merge_method: str | None = None,
        base_branch: str = "main",
        classic_linear: bool | None = None,
        protection: dict | None = None,
        protection_status: str | None = None,
        protection_failure: str | None = None,
        rules_pages: list[list[dict]] | None = None,
        rules_status: str = "0",
        undeclared_lane: bool = False,
        ready_issue: str | None = None,
    ):
        env = os.environ.copy()
        env.update(
            LAND_AGENT="kiat-luna",
            LAND_TEST_GATE_LOG=bash_path(self.gate_log),
            LAND_TEST_GH_LOG=bash_path(self.gh_log),
            LAND_TEST_GATE_STATUS=gate_status,
            LAND_TEST_STATE=state,
            LAND_TEST_MERGE_SHA="b" * 40,
            LAND_TEST_MERGE_REQUEST_STATUS=merge_request_status,
            LAND_TEST_MERGE_FAILURE=merge_failure,
            LAND_TEST_MERGE_MESSAGE=merge_message,
            LAND_TEST_REVIEWED=reviewed.lower(),
            AI_TEAM_TEST_ORIGIN_REPO="example/canonical",
            LAND_TEST_STACKED=stacked,
            LAND_TEST_ALLOW_MERGE=allow_merge,
            LAND_TEST_ALLOW_SQUASH=allow_squash,
            LAND_TEST_ALLOW_REBASE=allow_rebase,
            LAND_TEST_SETTINGS_STATUS=settings_status,
            LAND_TEST_BASE_BRANCH=base_branch,
            LAND_TEST_PROTECTION_STATUS=(
                protection_status
                if protection_status is not None
                else ("0" if classic_linear is not None or protection is not None else "404")
            ),
            LAND_TEST_PROTECTION=json.dumps(
                protection if protection is not None else {
                    "required_linear_history": {"enabled": classic_linear}
                }
            ),
            LAND_TEST_PROTECTION_FAILURE=(
                CANONICAL_UNPROTECTED_RESPONSE
                if protection_failure is None else protection_failure
            ),
            LAND_TEST_RULES_PAGES=json.dumps(
                rules_pages if rules_pages is not None else [[]]
            ),
            LAND_TEST_RULES_STATUS=rules_status,
            PATH=f"{self.cwd / 'bin'}{os.pathsep}{env.get('PATH', '')}",
        )
        if trusted_bot:
            env.update(
                LAND_TEST_TITLE="Bump Alpine.js from 3.16.2 to 3.16.3",
                LAND_TEST_BODY="Generated dependency update.",
                LAND_TEST_BRANCH="dependabot/npm_and_yarn/alpinejs-3.16.3",
                LAND_TEST_LABELS=json.dumps([{"name": "dependencies"}]),
                LAND_TEST_IDENTITY=json.dumps({
                    "user": {"id": 49699333, "login": "dependabot[bot]", "type": "Bot"},
                    "head": {"repo": {"id": 100}},
                    "base": {"repo": {"id": 100}},
                }),
            )
        else:
            env.update(
                LAND_TEST_TITLE="Fix lane (#42)",
                LAND_TEST_BODY="Closes #42",
                LAND_TEST_BRANCH="agent/author-issue-42",
                LAND_TEST_LABELS=json.dumps([
                    {"name": "agent:author"},
                    {"name": "task:review"},
                ]),
                LAND_TEST_IDENTITY=json.dumps({
                    "user": {"id": 1, "login": "human-author", "type": "User"},
                    "head": {"repo": {"id": 100}},
                    "base": {"repo": {"id": 100}},
                }),
            )
        if merge_method is not None:
            env["LAND_MERGE_METHOD"] = merge_method
        else:
            env.pop("LAND_MERGE_METHOD", None)
        if undeclared_lane:
            env.update(
                LAND_TEST_TITLE="Fix the thing",
                LAND_TEST_BODY="No closing reference here.",
                LAND_TEST_BRANCH="agent/author-fix",
            )
        if ready_issue is not None:
            env["READY_ISSUE"] = ready_issue
        else:
            env.pop("READY_ISSUE", None)
        if attributed:
            env["LAND_TEST_ATTRIBUTION"] = "**From:** kiat-luna — merged at " + "b" * 40
        else:
            env.pop("LAND_TEST_ATTRIBUTION", None)
        return run_with_bash_path(
            ["bash", bash_path(self.scripts / "land.sh"), "42", reviewed],
            stub_directory=self.cwd / "bin",
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


class LandMechanismTest(LandHarness):
    def test_failed_gate_never_merges_or_terminalizes(self):
        result = self.run_land(gate_status="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("-X PUT", gh_log)
        self.assertNotIn("issue edit", gh_log)
        self.assertNotIn("pr comment", gh_log)

    def test_failed_merge_endpoint_preserves_the_response_and_explains_protections(self):
        result = self.run_land(
            merge_request_status="1",
            merge_failure="gh: required approving review is missing (HTTP 405)",
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("merge request failed for PR #42", result.stderr)
        self.assertIn("required approving review is missing (HTTP 405)", result.stderr)
        self.assertIn("does not override GitHub branch protections", result.stderr)
        self.assertIn("separate eligible reviewer or automation", result.stderr)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("api -X PUT repos/example/canonical/pulls/42/merge", gh_log)
        self.assertNotIn("pr edit", gh_log)
        self.assertNotIn("issue edit", gh_log)
        self.assertNotIn("pr comment", gh_log)

    def test_unmerged_response_explains_protections_without_terminalizing(self):
        result = self.run_land(merge_message="Pull Request is not mergeable")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PR #42 was not merged: Pull Request is not mergeable", result.stderr)
        self.assertIn("does not override GitHub branch protections", result.stderr)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("pr edit", gh_log)
        self.assertNotIn("issue edit", gh_log)
        self.assertNotIn("pr comment", gh_log)

    def test_success_merges_terminalizes_both_surfaces_and_attributes(self):
        result = self.run_land()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PR #42 merged at " + "b" * 40, result.stdout)
        gate_log = self.gate_log.read_text(encoding="utf-8")
        self.assertIn("42 " + "a" * 40, gate_log)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("api -X PUT repos/example/canonical/pulls/42/merge", gh_log)
        self.assertIn("-f sha=" + "a" * 40, gh_log)
        self.assertIn("pr edit 42", gh_log)
        self.assertIn("issue edit 42", gh_log)
        self.assertIn("--add-label task:done", gh_log)
        self.assertIn("pr comment 42", gh_log)

    def test_rerun_on_merged_pr_retries_terminalization_without_second_merge(self):
        result = self.run_land(state="MERGED", attributed=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("-X PUT", gh_log)
        self.assertIn("pr edit 42", gh_log)
        self.assertIn("issue edit 42", gh_log)
        self.assertNotIn("pr comment 42", gh_log)

    def test_reviewed_sha_is_normalized_and_bound_to_the_merge_request(self):
        result = self.run_land(reviewed="A" * 40)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        gate_log = self.gate_log.read_text(encoding="utf-8")
        self.assertIn("42 " + "a" * 40, gate_log)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("-f sha=" + "a" * 40, gh_log)

    def test_trusted_dependabot_lane_merges_without_fabricating_an_issue(self):
        result = self.run_land(trusted_bot=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("trusted automated lane", result.stdout)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("api -X PUT repos/example/canonical/pulls/42/merge", gh_log)
        self.assertIn("-f sha=" + "a" * 40, gh_log)
        self.assertIn("pr edit 42", gh_log)
        self.assertIn("--add-label task:done", gh_log)
        self.assertNotIn("issue edit", gh_log)
        self.assertIn("pr comment 42", gh_log)

    def test_trusted_dependabot_merged_rerun_recovers_without_an_issue(self):
        result = self.run_land(state="MERGED", attributed=True, trusted_bot=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already merged; retrying terminalization", result.stdout)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("-X PUT", gh_log)
        self.assertIn("pr edit 42", gh_log)
        self.assertNotIn("issue edit", gh_log)
        self.assertNotIn("pr comment 42", gh_log)

    def test_a_stacked_pull_request_is_named_before_cleanup(self):
        # #69: landing and deleting the branch silently closed a stacked PR —
        # no merge, no comment, no notification, reviews left on a dead lane.
        result = self.run_land(stacked="#55")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("#55 is stacked on", result.stderr)
        self.assertIn("Do NOT delete that branch yet", result.stderr)
        self.assertIn("gh pr edit <number>", result.stderr)

    def test_several_stacked_pull_requests_are_all_named(self):
        result = self.run_land(stacked="#55, #56")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("#55, #56 are stacked on", result.stderr)

    def test_an_unstacked_landing_says_nothing_about_branches(self):
        result = self.run_land()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("stacked on", result.stderr)

    def test_the_warning_does_not_block_the_landing(self):
        # The deletion is not land.sh's to make; a stack is a warning, not a
        # refusal, or a correct landing becomes unrunnable.
        result = self.run_land(stacked="#55")
        self.assertEqual(result.returncode, 0)
        self.assertIn("task:done", self.gh_log.read_text(encoding="utf-8"))
    def test_an_undeclared_lane_is_still_refused(self):
        # #68: the refusal is correct and stays.
        result = self.run_land(undeclared_lane=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pass READY_ISSUE", result.stderr)

    def test_ready_issue_resolves_the_lane_land_refused(self):
        # #68: land.sh named READY_ISSUE as the remedy and then passed "" to
        # the deriver, so the remedy it printed was inert.
        result = self.run_land(undeclared_lane=True, ready_issue="46")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("pass READY_ISSUE", result.stderr)

    def test_a_declared_lane_is_unaffected_by_the_variable(self):
        result = self.run_land(ready_issue="42")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class LabelHygieneMechanismTest(unittest.TestCase):
    """The closed-issue query must expose non-terminal and contradictory lanes."""

    def test_recent_closed_nonterminal_and_contradictory_labels_are_reported(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            fixture = base / "closed.json"
            fixture.write_text(
                json.dumps([
                    {"number": 357, "title": "old review lane", "closedAt": "2026-08-27T00:00:00Z",
                     "labels": [{"name": "task:review"}]},
                    {"number": 317, "title": "contradictory lane", "closedAt": "2026-08-27T00:00:00Z",
                     "labels": [{"name": "task:done"}, {"name": "task:review"}]},
                    {"number": 400, "title": "clean lane", "closedAt": "2026-08-27T00:00:00Z",
                     "labels": [{"name": "task:done"}]},
                ]),
                encoding="utf-8",
            )
            log = base / "gh.log"
            gh = base / "gh"
            gh.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    printf '%s\\n' "$*" >>"$HYGIENE_TEST_LOG"
                    if [ "$1" = "issue" ] && [ "$2" = "list" ]; then
                      if [[ "$*" == *"--state closed"* ]]; then
                        cat "$HYGIENE_TEST_FIXTURE"
                      else
                        printf '[]\\n'
                      fi
                    fi
                    """
                ),
                encoding="utf-8",
            )
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env.update(
                HYGIENE_TEST_LOG=bash_path(log),
                HYGIENE_TEST_FIXTURE=bash_path(fixture),
                PATH=f"{base}{os.pathsep}{env.get('PATH', '')}",
            )
            result = run_with_bash_path(
                ["bash", bash_path(HYGIENE), "example/canonical"],
                stub_directory=base,
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("#357 CLOSED has non-terminal task label(s)", result.stdout)
            self.assertIn("#317 CLOSED has contradictory task labels", result.stdout)
            self.assertNotIn("#400", result.stdout)
            self.assertIn("--state closed", log.read_text(encoding="utf-8"))
            self.assertIn("--search closed:>=", log.read_text(encoding="utf-8"))


class MergeMethodTest(LandHarness):
    """#66 — the merge method belongs to the repository, not to this script.

    A repository that forbids merge commits answered a hardcoded
    `merge_method=merge` with a 405 *after* a full GATE: PASS, which reads as
    the gate having lied.
    """

    def merge_call(self):
        log = self.gh_log.read_text(encoding="utf-8")
        calls = [line for line in log.splitlines() if "-X PUT" in line]
        self.assertEqual(len(calls), 1, log)
        return calls[0]

    def test_merge_commit_is_preferred_when_allowed(self):
        result = self.run_land()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=merge", self.merge_call())

    def test_squash_is_used_when_merge_commits_are_forbidden(self):
        result = self.run_land(allow_merge="false")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=squash", self.merge_call())
        self.assertIn("effective methods", result.stderr)

    def test_rebase_is_the_last_resort(self):
        result = self.run_land(allow_merge="false", allow_squash="false")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=rebase", self.merge_call())

    def test_a_repository_allowing_nothing_is_refused_before_the_merge(self):
        result = self.run_land(
            allow_merge="false", allow_squash="false", allow_rebase="false"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("allow no common merge method", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_the_override_is_honoured_on_the_path_that_prints_it(self):
        """#68 is about a remedy the tool names and ignores; this one works."""
        result = self.run_land(allow_merge="false", merge_method="squash")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=squash", self.merge_call())

    def test_the_override_beats_a_repository_that_allows_everything(self):
        result = self.run_land(merge_method="rebase")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=rebase", self.merge_call())

    def test_a_bogus_override_is_refused_by_invocation(self):
        result = self.run_land(merge_method="fast-forward")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be merge, squash, or rebase", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_an_unreadable_repository_refuses_instead_of_guessing(self):
        result = self.run_land(settings_status="1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to guess merge policy", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_classic_linear_history_overrides_repository_merge_permission(self):
        """#95: this is the exact failure shape reproduced on People PR #96."""
        result = self.run_land(classic_linear=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=squash", self.merge_call())
        self.assertNotIn("-f merge_method=merge", self.merge_call())

    def test_explicitly_disabled_classic_linear_history_keeps_merge(self):
        """A valid false value is policy, not a jq parse failure."""
        result = self.run_land(classic_linear=False)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=merge", self.merge_call())

    def test_absent_classic_linear_history_keeps_merge(self):
        result = self.run_land(protection={})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=merge", self.merge_call())

    def test_all_matching_rulesets_are_intersected(self):
        result = self.run_land(rules_pages=[[{
            "type": "pull_request",
            "ruleset_id": 10,
            "parameters": {"allowed_merge_methods": ["merge", "squash"]},
        }, {
            "type": "pull_request",
            "ruleset_id": 20,
            "parameters": {"allowed_merge_methods": ["squash", "rebase"]},
        }]])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=squash", self.merge_call())

    def test_ruleset_linear_history_removes_merge(self):
        result = self.run_land(rules_pages=[[{
            "type": "required_linear_history", "ruleset_id": 10
        }]])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=squash", self.merge_call())

    def test_override_is_refused_when_classic_protection_forbids_it(self):
        result = self.run_land(classic_linear=True, merge_method="merge")

        self.assertEqual(result.returncode, 1)
        self.assertIn("LAND_MERGE_METHOD=merge is forbidden", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_override_is_refused_when_a_ruleset_forbids_it(self):
        result = self.run_land(merge_method="merge", rules_pages=[[{
            "type": "pull_request",
            "parameters": {"allowed_merge_methods": ["squash"]},
        }]])

        self.assertEqual(result.returncode, 1)
        self.assertIn("LAND_MERGE_METHOD=merge is forbidden", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_unreadable_ruleset_policy_fails_closed_even_with_override(self):
        result = self.run_land(merge_method="squash", rules_status="1")

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read active rulesets", result.stderr)
        self.assertIn("refusing to guess merge policy", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_unreadable_classic_protection_fails_closed(self):
        result = self.run_land(
            protection_status="403",
            protection_failure="gh: forbidden (HTTP 403)",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read classic protection", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_live_concatenated_unprotected_response_is_accepted(self):
        result = self.run_land(
            protection_status="404",
            protection_failure=CANONICAL_UNPROTECTED_RESPONSE,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=merge", self.merge_call())

    def test_canonical_json_without_gh_diagnostic_is_accepted(self):
        result = self.run_land(
            protection_status="404",
            protection_failure=CANONICAL_UNPROTECTED_JSON,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=merge", self.merge_call())

    def test_reordered_canonical_json_is_accepted(self):
        result = self.run_land(
            protection_status="404",
            protection_failure=REORDERED_UNPROTECTED_JSON,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-f merge_method=merge", self.merge_call())

    def test_duplicate_404_members_fail_closed(self):
        for response in DUPLICATE_UNPROTECTED_RESPONSES:
            with self.subTest(response=response):
                self.gh_log.write_text("", encoding="utf-8")
                result = self.run_land(
                    protection_status="404", protection_failure=response
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("cannot read classic protection", result.stderr)
                self.assertNotIn(
                    "-X PUT", self.gh_log.read_text(encoding="utf-8")
                )

    def test_duplicate_404_members_fail_closed_even_with_override(self):
        for response in DUPLICATE_UNPROTECTED_RESPONSES:
            with self.subTest(response=response):
                self.gh_log.write_text("", encoding="utf-8")
                result = self.run_land(
                    merge_method="squash",
                    protection_status="404",
                    protection_failure=response,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("cannot read classic protection", result.stderr)
                self.assertNotIn(
                    "-X PUT", self.gh_log.read_text(encoding="utf-8")
                )

    def test_ambiguous_classic_linear_history_fails_closed(self):
        result = self.run_land(protection={
            "required_linear_history": {"enabled": "yes"}
        })

        self.assertEqual(result.returncode, 2)
        self.assertIn("ambiguous required_linear_history", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_generic_404_classic_protection_fails_closed(self):
        result = self.run_land(
            protection_status="404",
            protection_failure='{"message":"Not Found","status":"404"}\n'
            "gh: Not Found (HTTP 404)",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read classic protection", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_generic_404_fails_closed_even_with_override(self):
        result = self.run_land(
            merge_method="squash",
            protection_status="404",
            protection_failure='{"message":"Not Found","status":"404"}\n'
            "gh: Not Found (HTTP 404)",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read classic protection", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_canonical_404_with_extra_or_contradictory_evidence_fails_closed(self):
        poisoned_responses = [
            CANONICAL_UNPROTECTED_JSON + "not-json",
            CANONICAL_UNPROTECTED_JSON
            + '{"message":"Not Found","status":"404"}',
            CANONICAL_UNPROTECTED_JSON + "gh: Forbidden (HTTP 403)",
        ]
        for response in poisoned_responses:
            with self.subTest(response=response):
                self.gh_log.write_text("", encoding="utf-8")
                result = self.run_land(
                    protection_status="404", protection_failure=response
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("cannot read classic protection", result.stderr)
                self.assertNotIn(
                    "-X PUT", self.gh_log.read_text(encoding="utf-8")
                )

    def test_poisoned_canonical_404_fails_closed_even_with_override(self):
        poisoned_responses = [
            CANONICAL_UNPROTECTED_JSON + "not-json",
            CANONICAL_UNPROTECTED_JSON
            + '{"message":"Not Found","status":"404"}',
            CANONICAL_UNPROTECTED_JSON + "gh: Forbidden (HTTP 403)",
        ]
        for response in poisoned_responses:
            with self.subTest(response=response):
                self.gh_log.write_text("", encoding="utf-8")
                result = self.run_land(
                    merge_method="squash",
                    protection_status="404",
                    protection_failure=response,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("cannot read classic protection", result.stderr)
                self.assertNotIn(
                    "-X PUT", self.gh_log.read_text(encoding="utf-8")
                )

    def test_malformed_pull_request_rule_fails_closed(self):
        result = self.run_land(rules_pages=[[{
            "type": "pull_request", "parameters": {}
        }]])

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid allowed_merge_methods", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))

    def test_disjoint_rulesets_refuse_before_the_merge(self):
        result = self.run_land(rules_pages=[[{
            "type": "pull_request",
            "parameters": {"allowed_merge_methods": ["merge"]},
        }, {
            "type": "pull_request",
            "parameters": {"allowed_merge_methods": ["squash"]},
        }]])

        self.assertEqual(result.returncode, 1)
        self.assertIn("allow no common merge method", result.stderr)
        self.assertNotIn("-X PUT", self.gh_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
