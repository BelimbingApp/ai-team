import re
import unittest
from pathlib import Path


README = Path(__file__).parents[1] / "README.md"


class ReadmeOnboardingTest(unittest.TestCase):
    def test_readme_stays_short_and_runtime_neutral(self):
        document = README.read_text(encoding="utf-8")

        self.assertLessEqual(len(document.split()), 4_000)
        self.assertNotRegex(document, r"(?<![\w/])#\d+\b")
        self.assertNotIn("cross-session messaging", document.lower())
        self.assertIn("direct agent messaging", document)

    def test_readme_distinguishes_package_and_adopter_script_paths(self):
        document = README.read_text(encoding="utf-8")

        self.assertIn("`scripts/`", document)
        self.assertIn("`docs/ai-team/scripts/`", document)
        self.assertIn("`.ai-team/project-orient.sh`", document)
        self.assertIn("`templates/project-orient.sh`", document)
        self.assertIn("git subtree add --prefix=docs/ai-team", document)
        self.assertIn("`.agents/skills/ai-team/`", document)
        self.assertIn("Claude Code", document)


if __name__ == "__main__":
    unittest.main()
