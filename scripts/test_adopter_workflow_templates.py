import unittest
from pathlib import Path


TEMPLATES = Path(__file__).parents[1] / "templates"
MECHANISMS = (TEMPLATES / "mechanisms.yml").read_text(encoding="utf-8")
SWEEP = (TEMPLATES / "blocked-by-sweep.yml").read_text(encoding="utf-8")


class AdopterWorkflowTemplateTest(unittest.TestCase):
    def test_mechanism_workflow_is_unfiltered_and_runs_on_default_branch_and_prs(self):
        self.assertIn("push:\n    branches:\n      - main", MECHANISMS)
        self.assertIn("  pull_request:\n", MECHANISMS)
        self.assertIn("  workflow_dispatch:\n", MECHANISMS)
        self.assertNotIn("paths:", MECHANISMS)
        self.assertIn(
            "python3 -m unittest discover -s docs/ai-team/scripts -p 'test_*.py'",
            MECHANISMS,
        )
        self.assertIn(
            "shellcheck --severity=error docs/ai-team/scripts/*.sh", MECHANISMS
        )
        self.assertNotIn("issues: write", MECHANISMS)

    def test_sweep_is_schedule_only_and_scopes_issue_write_to_its_job(self):
        self.assertIn('schedule:\n    - cron: "17,47 * * * *"', SWEEP)
        self.assertIn("  workflow_dispatch:\n", SWEEP)
        self.assertNotIn("pull_request:", SWEEP)
        self.assertEqual(SWEEP.count("issues: write"), 1)
        self.assertIn(
            "run: python3 docs/ai-team/scripts/blocked_by_sweep.py", SWEEP
        )
        self.assertIn("GITHUB_REPOSITORY: ${{ github.repository }}", SWEEP)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", SWEEP)

    def test_install_guide_names_all_adopter_owned_templates(self):
        document = (TEMPLATES.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "cp docs/ai-team/templates/mechanisms.yml .github/workflows/ai-team-mechanisms.yml",
            document,
        )
        self.assertIn(
            "cp docs/ai-team/templates/blocked-by-sweep.yml .github/workflows/ai-team-blocked-by-sweep.yml",
            document,
        )
        self.assertIn(
            "cp docs/ai-team/templates/independent-review.yml .github/workflows/ai-team-independent-review.yml",
            document,
        )
        self.assertIn("issues: write", document)
        self.assertIn(
            "cp docs/ai-team/templates/package-refresh.sh .ai-team/package-refresh.sh",
            document,
        )
        self.assertNotIn("cp docs/ai-team/templates/activate.sh", document)


if __name__ == "__main__":
    unittest.main()
