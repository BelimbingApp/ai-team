import re
import unittest
from pathlib import Path


README = Path(__file__).parents[2] / "README.md"


class ReadmeOnboardingTest(unittest.TestCase):
    def test_readme_stays_short_and_runtime_neutral(self):
        document = README.read_text(encoding="utf-8")

        self.assertLessEqual(len(document.split()), 4_000)
        self.assertNotRegex(document, r"(?<![\w/])#\d+\b")
        self.assertNotIn("cross-session messaging", document.lower())
        self.assertIn("direct agent messaging", document)

    def test_readme_distinguishes_package_and_adopter_script_paths(self):
        document = README.read_text(encoding="utf-8")

        self.assertIn("`package/scripts/`", document)
        self.assertIn("`docs/ai-team/scripts/`", document)
        self.assertIn("`.ai-team/project-orient.sh`", document)
        self.assertIn("`package/templates/project-orient.sh`", document)
        self.assertIn("git subtree add --prefix=docs/ai-team", document)
        self.assertIn("`.agents/skills/ai-team/`", document)
        self.assertIn("Claude Code", document)

    def test_readme_package_repository_commands_use_the_package_prefix(self):
        # #26 moved scripts/ and templates/ under package/. A command example
        # under a "# Package repository" heading names where this repository's
        # own mechanisms actually live now — a bare `scripts/foo.sh` here is
        # stale and points nowhere once someone tries to run it (caught in
        # review on #31: three code blocks and four inline references still
        # named the pre-move path after the directories had already moved).
        document = README.read_text(encoding="utf-8")

        stale = re.findall(r"(?<![/\w])scripts/[A-Za-z_]+\.(?:sh|py)\b", document)
        self.assertEqual(
            stale, [], f"bare (non-package-prefixed) scripts/ reference(s) survive the #26 move: {stale}"
        )

        stale_templates = re.findall(r"(?<![/\w])templates/project-orient\.sh\b", document)
        self.assertEqual(
            stale_templates,
            [],
            f"bare (non-package-prefixed) templates/ reference(s) survive the #26 move: {stale_templates}",
        )


if __name__ == "__main__":
    unittest.main()
