import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


ORIENT = Path(__file__).with_name("orient.sh")
SCRIPT_NAMES = (
    "orient.sh",
    "_lane_issue.sh",
    "_default_branch.sh",
    "halt_status.sh",
    "board.sh",
    "label_hygiene.sh",
)


class OrientStewardMechanismTest(unittest.TestCase):
    """Exercise the assembled orient.sh steward output for every cardinality state."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.base = base
        self.scripts = base / "scripts"
        self.scripts.mkdir()
        for name in SCRIPT_NAMES:
            source = ORIENT.with_name(name)
            destination = self.scripts / name
            shutil.copy2(source, destination)
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)

        self.bin = base / "bin"
        self.bin.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(base)], check=True)
        self.write_gh_stub()

    def tearDown(self):
        self.dir.cleanup()

    def write_gh_stub(self):
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                case "$1 $2" in
                  "repo view")
                    if [[ "$*" == *defaultBranchRef* ]]; then printf 'main\\n'
                    else printf 'example/canonical\\n'; fi
                    ;;
                  "issue list")
                    if [[ "$*" == *"--label ops:halt"* ]]; then
                      printf ''
                    elif [[ "$*" == *"--label ops:steward"* ]]; then
                      printf '%s' "${ORIENT_TEST_STEWARDS:-}"
                    else
                      printf '[]\\n'
                    fi
                    ;;
                  "pr list"|"api")
                    printf '[]\\n'
                    ;;
                  *)
                    printf '[]\\n'
                    ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def run_orient(self, stewards: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            ORIENT_TEST_STEWARDS=stewards,
            AI_TEAM_TEST_ORIGIN_REPO="example/canonical",
            PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
        )
        return run_with_bash_path(
            ["bash", bash_path(self.scripts / "orient.sh")],
            stub_directory=self.bin,
            cwd=self.base,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_orientation(self, stewards: str, expected: str, unexpected: str = ""):
        result = self.run_orient(stewards)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(expected, result.stdout)
        if unexpected:
            self.assertNotIn(unexpected, result.stdout)

    def test_zero_active_stewards_is_explicitly_invalid(self):
        self.assert_orientation(
            "",
            "WARNING expected exactly one active ops:steward issue (found 0)",
        )

    def test_exactly_one_steward_with_one_agent_is_clean(self):
        self.assert_orientation(
            "380\tagent:opus-5\t1\tappointment",
            "#380 [agent:opus-5] appointment",
            "active steward must carry exactly one",
        )

    def test_exactly_one_steward_prints_appointment_identity_note(self):
        # #51: orient must remind substitutes that the appointment label is not
        # their **From:** identity.
        self.assert_orientation(
            "468\tagent:fable\t1\tsteward appointment",
            "NOTE: agent:fable on #468 is the APPOINTMENT, not your **From:** identity.",
        )

    def test_multiple_active_stewards_are_reported(self):
        self.assert_orientation(
            "380\tagent:opus-5\t1\tfirst\n381\tagent:sol\t1\tsecond",
            "WARNING expected exactly one active ops:steward issue (found 2)",
        )

    def test_steward_without_agent_is_reported(self):
        self.assert_orientation(
            "380\t-\t0\tappointment",
            "WARNING #380 active steward must carry exactly one agent:* label (found 0)",
        )

    def test_steward_with_multiple_agents_is_reported(self):
        self.assert_orientation(
            "380\tagent:opus-5, agent:sol\t2\tappointment",
            "WARNING #380 active steward must carry exactly one agent:* label (found 2)",
        )

    def test_the_active_steward_query_is_scoped_to_open_state(self):
        # #383: retirement never removes ops:steward — the label is a
        # permanent historical record, and --state open is the entire
        # contract for "counts as active." A closed appointment retaining
        # the label (#309, #343, #365 all do) must never be discovered here.
        # This test protects the flag directly rather than only trusting the
        # stub's behavior, since the stub cannot itself prove production
        # asked gh to filter by state.
        text = ORIENT.read_text(encoding="utf-8")
        anchor = text.index('--label "ops:steward"')
        line_start = text.rindex("\n", 0, anchor) + 1
        line = text[line_start:text.index("\n", anchor)]
        self.assertIn("--state open", line)


if __name__ == "__main__":
    unittest.main()
