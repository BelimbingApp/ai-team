import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import run_with_bash_path


SCRIPT = Path(__file__).with_name("halt_status.sh")


class HaltStatusTest(unittest.TestCase):
    def run_status(self, mode: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / "gh"
            gh.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    case "$HALT_TEST_MODE" in
                      none) exit 0 ;;
                      active) printf '%s\\n' '  HALT #42 — maintenance' ;;
                      failure) exit 1 ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["HALT_TEST_MODE"] = mode
            return run_with_bash_path(
                ["bash", str(SCRIPT), "example/repository"],
                stub_directory=Path(directory),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_no_halt_allows_orientation(self):
        result = self.run_status("none")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no halt active", result.stdout)

    def test_active_halt_stops_orientation(self):
        result = self.run_status("active")
        self.assertEqual(result.returncode, 3)
        self.assertIn("STAND DOWN", result.stdout)
        self.assertIn("HALT #42", result.stdout)

    def test_query_failure_fails_closed(self):
        result = self.run_status("failure")
        self.assertEqual(result.returncode, 2)
        self.assertIn("HALT STATUS UNKNOWN", result.stdout)


if __name__ == "__main__":
    unittest.main()
