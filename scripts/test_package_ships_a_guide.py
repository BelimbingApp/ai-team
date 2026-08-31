import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(
    subprocess.run(
        ["git", "-C", str(Path(__file__).parent), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
).resolve()


def committed_package_paths() -> set[str]:
    """The exact set of paths `git subtree split --prefix=package` would carry
    into a `package-mount` publish, read from the current commit rather than
    from the working tree — this is what an adopter's mount actually gets,
    independent of any uncommitted local edit."""

    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "ls-tree", "-rz", "--name-only", "HEAD", "--", "package"],
        text=True,
        capture_output=True,
        check=True,
    )
    return {entry for entry in result.stdout.split("\0") if entry}


class PackageShipsAGuideTest(unittest.TestCase):
    """#26 review (codex-gpt-5): the split tree carried only LICENSE, scripts/,
    templates/ — no README.md — so a real `package-mount` gave an adopter no
    `docs/ai-team/README.md`, and the shipped `scripts/README.md`'s own
    `../README.md` reference pointed at nothing. Checked from the committed
    tree, not the working tree, so a fix that only exists uncommitted still
    fails this the same way the regression did."""

    def test_the_split_tree_includes_a_guide(self):
        paths = committed_package_paths()

        self.assertIn(
            "package/README.md",
            paths,
            "package/ (and therefore any package-mount split of it) ships no README.md",
        )

    def test_the_shipped_guide_is_not_a_stub(self):
        guide = REPOSITORY_ROOT / "package" / "README.md"
        self.assertTrue(guide.is_file())

        text = guide.read_text(encoding="utf-8")
        self.assertGreater(len(text.split()), 200)
        self.assertIn("AI Team", text)


if __name__ == "__main__":
    unittest.main()
