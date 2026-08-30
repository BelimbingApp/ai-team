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
HYGIENE = Path(__file__).with_name("label_hygiene.sh")


class LandMechanismTest(unittest.TestCase):
    """Hermetic regressions for the gate-to-terminal lane transition."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.scripts = base / "scripts"
        self.scripts.mkdir()
        for path in (SCRIPT, LANE):
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
                      '{number:42,title:"Fix lane (#42)",body:"Closes #42",headRefName:"agent/author-issue-42",labels:[{name:"agent:author"},{name:"task:review"}],isDraft:false,state:$state,mergeCommit:(if $state == "MERGED" then {oid:$sha} else null end),comments:(if $attr == "" then [] else [{body:$attr}] end)}'
                    ;;
                  "api -X")
                    if [ "${3:-}" = "PUT" ]; then
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

    def run_land(self, *, gate_status: str = "0", state: str = "OPEN", attributed: bool = False):
        env = os.environ.copy()
        env.update(
            LAND_AGENT="kiat-luna",
            LAND_TEST_GATE_LOG=bash_path(self.gate_log),
            LAND_TEST_GH_LOG=bash_path(self.gh_log),
            LAND_TEST_GATE_STATUS=gate_status,
            LAND_TEST_STATE=state,
            LAND_TEST_MERGE_SHA="b" * 40,
            PATH=f"{self.cwd / 'bin'}{os.pathsep}{env.get('PATH', '')}",
        )
        if attributed:
            env["LAND_TEST_ATTRIBUTION"] = "**From:** kiat-luna — merged at " + "b" * 40
        else:
            env.pop("LAND_TEST_ATTRIBUTION", None)
        return run_with_bash_path(
            ["bash", str(self.scripts / "land.sh"), "42", "a" * 40],
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

    def test_success_merges_terminalizes_both_surfaces_and_attributes(self):
        result = self.run_land()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PR #42 merged at " + "b" * 40, result.stdout)
        gate_log = self.gate_log.read_text(encoding="utf-8")
        self.assertIn("42 " + "a" * 40, gate_log)
        gh_log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("api -X PUT repos/example/canonical/pulls/42/merge", gh_log)
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
                ["bash", str(HYGIENE), "example/canonical"],
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
