import re
import unittest
from pathlib import Path


README = Path(__file__).parents[1] / "README.md"
SCRIPTS_README = Path(__file__).parent / "README.md"
ORIENT = Path(__file__).parent / "orient.sh"
CLAIM = Path(__file__).parent / "claim.sh"


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

    def test_start_work_routes_adopters_directly_to_read_only_orientation(self):
        document = README.read_text(encoding="utf-8")
        start_work = document.split("## Start work", 1)[1].split("---", 1)[0]

        self.assertIn("# Adopting repository\ndocs/ai-team/scripts/orient.sh", start_work)
        self.assertNotIn(".ai-team/activate.sh", start_work)
        self.assertNotIn("package-refresh", document)

    def test_routine_orientation_and_claim_do_not_run_the_mechanism_suite(self):
        for command in (ORIENT, CLAIM):
            source = command.read_text(encoding="utf-8")
            self.assertNotIn("unittest discover", source)
            self.assertNotIn("test_*.py", source)


class ScriptsReadmeOnboardingTest(unittest.TestCase):
    def test_the_windows_test_command_has_both_the_package_and_adopter_forms(self):
        # #26 review, codex-fasttrack: the Linux/macOS commands were fixed
        # to show both forms, but Windows kept only the home-repository
        # command — a Windows adopter following the shipped guide had no
        # runnable command for their own mount at all.
        document = SCRIPTS_README.read_text(encoding="utf-8")
        windows_block = document.split("# Windows", 1)[1].split("```", 1)[0]

        self.assertIn("-s package/scripts", windows_block)
        self.assertIn("-s docs/ai-team/scripts", windows_block)


if __name__ == "__main__":
    unittest.main()
