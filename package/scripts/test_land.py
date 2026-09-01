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


class LandMechanismTest(unittest.TestCase):
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
                      --argjson labels "$LAND_TEST_LABELS" \\
                      '{number:42,title:$title,body:$body,headRefName:$branch,labels:$labels,isDraft:false,state:$state,mergeCommit:(if $state == "MERGED" then {oid:$sha} else null end),comments:(if $attr == "" then [] else [{body:$attr}] end)}'
                    ;;
                  "api repos/example/canonical/pulls/42")
                    printf '%s\\n' "$LAND_TEST_IDENTITY"
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


if __name__ == "__main__":
    unittest.main()
