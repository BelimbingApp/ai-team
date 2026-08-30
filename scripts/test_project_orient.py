import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from _test_support import run_with_bash_path


SCRIPTS = Path(__file__).parent
TEMPLATES = SCRIPTS.parent / "templates"


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


class ProjectOrientCountsTest(unittest.TestCase):
    """#10: the hook reported one tracked file and no test modules, because
    `git ls-tree` without -r names the tree entry rather than its contents."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        root = Path(self.workspace.name)

        self.origin = root / "origin.git"
        self.checkout = root / "checkout"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.checkout)], check=True
        )

        scripts = self.checkout / "scripts"
        scripts.mkdir()
        # A tree the counts can be checked against: shell mechanisms, test
        # modules, and one file that is neither.
        for name in ("orient.sh", "gate.sh", "claim.sh"):
            (scripts / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        for name in ("test_gate.py", "test_claim.py"):
            (scripts / name).write_text("", encoding="utf-8")
        (scripts / "README.md").write_text("", encoding="utf-8")
        # A path outside scripts/ must not be counted.
        (self.checkout / "README.md").write_text("", encoding="utf-8")

        # Placed outside scripts/, matching the real mechanism (#8): the hook
        # lives at .ai-team/ in the adopting repository's own root, copied
        # from templates/project-orient.sh, never inside the mounted package.
        dot_ai_team = self.checkout / ".ai-team"
        dot_ai_team.mkdir()
        hook = dot_ai_team / "project-orient.sh"
        hook.write_bytes((TEMPLATES / "project-orient.sh").read_bytes())
        hook.chmod(0o755)
        self.hook = hook

        git(self.checkout, "add", "-A")
        git(
            self.checkout,
            "-c", "user.name=ai-team-test",
            "-c", "user.email=ai-team-test@example.invalid",
            "commit", "-qm", "fixture",
        )
        git(self.checkout, "remote", "add", "origin", str(self.origin))
        git(self.checkout, "push", "-q", "origin", "main")
        git(self.checkout, "fetch", "-q", "origin", "main")

    def run_hook(self) -> str:
        environment = os.environ.copy()
        # orient.sh resolves the branch once and exports it; the hook no
        # longer sources _default_branch.sh itself (it has no relative path
        # back to scripts/ once copied out to .ai-team/), so this is what a
        # real invocation via orient.sh supplies.
        environment["AI_TEAM_DEFAULT_BRANCH"] = "main"
        stubs = Path(self.workspace.name) / "stubs"
        stubs.mkdir(exist_ok=True)

        result = run_with_bash_path(
            [str(self.hook)],
            stub_directory=stubs,
            env=environment,
            cwd=str(self.checkout),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def counted(self, output: str, label: str) -> int:
        match = re.search(rf"^\s+{label}\s+(\d+)\s", output, re.MULTILINE)
        self.assertIsNotNone(match, f"no {label!r} line in:\n{output}")
        return int(match.group(1))

    def test_the_counts_agree_with_git(self):
        tracked = [
            path
            for path in git(self.checkout, "ls-files", "scripts").splitlines()
            if path
        ]
        expected_tests = [
            path for path in tracked if path.startswith("scripts/test_")
        ]

        output = self.run_hook()

        self.assertEqual(self.counted(output, "scripts/"), len(tracked))
        self.assertEqual(self.counted(output, "tests"), len(expected_tests))

    def test_the_counts_are_not_the_pre_fix_constants(self):
        # The bug was silent because 1 and 0 are plausible-looking numbers.
        # Pin them as wrong for a tree that has more than one file.
        output = self.run_hook()

        self.assertGreater(self.counted(output, "scripts/"), 1)
        self.assertGreater(self.counted(output, "tests"), 0)


if __name__ == "__main__":
    unittest.main()
