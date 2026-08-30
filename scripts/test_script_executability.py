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
# templates/ ships the project hook (#8) — an adopter copies it out and runs
# it as a shell mechanism exactly like anything under scripts/, so it needs
# the same committed-executable guarantee.
TEMPLATES_DIRECTORY = SCRIPT_DIRECTORY.parent / "templates"
TEMPLATES_PATHSPEC = TEMPLATES_DIRECTORY.relative_to(REPOSITORY_ROOT).as_posix()


def committed_shell_modes(pathspec: str) -> dict[Path, str]:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "ls-tree", "-rz", "HEAD", "--", pathspec],
        text=True,
        capture_output=True,
        check=True,
    )
    return {
        Path(path): metadata.split()[0]
        for entry in result.stdout.split("\0")
        if entry
        for metadata, path in [entry.split("\t", maxsplit=1)]
    }


class ScriptExecutabilityTest(unittest.TestCase):
    def assert_directory_is_committed_executable(self, directory: Path, pathspec: str) -> None:
        modes = committed_shell_modes(pathspec)
        shipped_shell_scripts = {path for path in modes if path.suffix == ".sh"}
        expected_shell_scripts = {
            path.relative_to(REPOSITORY_ROOT) for path in directory.glob("*.sh")
        }

        self.assertEqual(shipped_shell_scripts, expected_shell_scripts)
        self.assertTrue(shipped_shell_scripts)
        for path in shipped_shell_scripts:
            with self.subTest(path=path):
                self.assertEqual(modes[path], "100755")

    def test_all_shipped_shell_mechanisms_are_committed_executable(self):
        self.assert_directory_is_committed_executable(SCRIPT_DIRECTORY, SCRIPT_PATHSPEC)

    def test_all_shipped_templates_are_committed_executable(self):
        self.assert_directory_is_committed_executable(TEMPLATES_DIRECTORY, TEMPLATES_PATHSPEC)


if __name__ == "__main__":
    unittest.main()
