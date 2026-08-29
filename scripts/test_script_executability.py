import subprocess
import unittest
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).parent.resolve()
# The package is the repository here and a subdirectory of one wherever it is
# mounted, so neither the root nor the pathspec can be a literal. Ask git for
# the root and derive the pathspec from this file's own position under it.
REPOSITORY_ROOT = Path(
    subprocess.run(
        ["git", "-C", str(SCRIPT_DIRECTORY), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
).resolve()
SCRIPT_PATHSPEC = SCRIPT_DIRECTORY.relative_to(REPOSITORY_ROOT).as_posix()


class ScriptExecutabilityTest(unittest.TestCase):
    def test_all_shipped_shell_mechanisms_are_committed_executable(self):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(REPOSITORY_ROOT),
                "ls-tree",
                "-rz",
                "HEAD",
                "--",
                SCRIPT_PATHSPEC,
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        modes = {
            Path(path): metadata.split()[0]
            for entry in result.stdout.split("\0")
            if entry
            for metadata, path in [entry.split("\t", maxsplit=1)]
        }
        shipped_shell_scripts = {
            path for path in modes if path.suffix == ".sh"
        }
        expected_shell_scripts = {
            path.relative_to(REPOSITORY_ROOT)
            for path in SCRIPT_DIRECTORY.glob("*.sh")
        }

        self.assertEqual(shipped_shell_scripts, expected_shell_scripts)
        self.assertTrue(shipped_shell_scripts)
        for path in shipped_shell_scripts:
            with self.subTest(path=path):
                self.assertEqual(modes[path], "100755")


if __name__ == "__main__":
    unittest.main()
