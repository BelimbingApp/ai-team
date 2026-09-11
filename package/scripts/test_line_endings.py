import subprocess
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
REPO = PACKAGE.parent


class LineEndingTest(unittest.TestCase):
    """
    #122: package/ is republished by package-split.yml as the `package-mount`
    branch, whose root IS package/'s contents, and adopters mount that branch.
    A shell script that reaches an adopter with CRLF fails at the shebang with
    "/usr/bin/env: 'bash\\r': No such file or directory" -- a message that points
    at bash rather than at the file, which is what makes it expensive to read.
    """

    def tracked(self, prefix):
        out = subprocess.run(
            ["git", "ls-files", "-z", prefix],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout
        return [p for p in out.split("\0") if p]

    def test_no_tracked_package_file_carries_a_carriage_return(self):
        offenders = []
        for path in self.tracked("package"):
            blob = (REPO / path).read_bytes()
            if b"\r" in blob:
                offenders.append(path)

        self.assertEqual(offenders, [], f"CRLF in tracked package files: {offenders}")

    def test_package_pins_lf_so_the_published_mount_inherits_it(self):
        # The attribute has to live INSIDE package/. A root-level .gitattributes
        # governs this repository but is not carried by `git subtree split
        # --prefix=package`, so it would protect main and leave every adopter
        # checkout unguarded -- the wrong half of the problem.
        attributes = PACKAGE / ".gitattributes"
        self.assertTrue(attributes.is_file(), "package/.gitattributes is missing")
        self.assertIn("eol=lf", attributes.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
