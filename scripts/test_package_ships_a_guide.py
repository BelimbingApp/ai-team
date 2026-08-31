import subprocess
import unittest
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).parent.resolve()
# The package root is `package/` in this repository and `docs/ai-team/` once
# mounted, so — same reasoning as test_script_executability.py's
# SCRIPT_PATHSPEC — neither the root nor the pathspec can be a literal.
PACKAGE_DIRECTORY = SCRIPT_DIRECTORY.parent
REPOSITORY_ROOT = Path(
    subprocess.run(
        ["git", "-C", str(SCRIPT_DIRECTORY), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
).resolve()
PACKAGE_PATHSPEC = PACKAGE_DIRECTORY.relative_to(REPOSITORY_ROOT).as_posix()


def committed_package_paths() -> set[str]:
    """The exact set of paths `git subtree split --prefix=package` would carry
    into a `package-mount` publish, read from the current commit rather than
    from the working tree — this is what an adopter's mount actually gets,
    independent of any uncommitted local edit. Paths come back relative to
    PACKAGE_PATHSPEC's parent, i.e. as `package/README.md` here and
    `docs/ai-team/README.md` once mounted, matching where this test itself
    is running from either way."""

    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "ls-tree", "-rz", "--name-only", "HEAD", "--", PACKAGE_PATHSPEC],
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
    fails this the same way the regression did.

    A first cut of this test hardcoded the `package` pathspec (#26 review,
    codex-fasttrack): correct here, empty in the mounted layout where the
    real prefix is `docs/ai-team`, so the test could not actually validate
    what ships to an adopter. Derived dynamically now, the same way
    test_script_executability.py already had to solve this."""

    def test_the_split_tree_includes_a_guide(self):
        paths = committed_package_paths()

        self.assertIn(
            f"{PACKAGE_PATHSPEC}/README.md",
            paths,
            f"{PACKAGE_PATHSPEC}/ (and therefore any package-mount split of it) ships no README.md",
        )

    def test_the_shipped_guide_is_not_a_stub(self):
        guide = PACKAGE_DIRECTORY / "README.md"
        self.assertTrue(guide.is_file())

        text = guide.read_text(encoding="utf-8")
        self.assertGreater(len(text.split()), 200)
        self.assertIn("AI Team", text)


if __name__ == "__main__":
    unittest.main()
