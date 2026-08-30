import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE_WORKFLOW = ROOT / ".github" / "workflows" / "independent-review.yml"
ADOPTER_TEMPLATE = ROOT / "templates" / "independent-review.yml"


class IndependentReviewWorkflowTest(unittest.TestCase):
    def test_package_and_adopter_workflows_share_the_trusted_shape(self):
        package = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
        adopter = ADOPTER_TEMPLATE.read_text(encoding="utf-8")

        for workflow in (package, adopter):
            self.assertIn("pull_request_target:", workflow)
            self.assertIn("pull_request_review:", workflow)
            self.assertNotIn("  pull_request:\n", workflow)
            self.assertIn("actions/checkout@v5", workflow)
            self.assertIn("ref: ${{ github.event.repository.default_branch }}", workflow)
            self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
            self.assertIn("github.event.pull_request.number", workflow)
            self.assertIn("github.event.pull_request.head.sha", workflow)
            self.assertIn("id: resolve", workflow)
            self.assertIn("steps.resolve.outputs.present == 'true'", workflow)
            self.assertIn("steps.resolve.outputs.present == 'false'", workflow)
            self.assertIn('echo "present=true" >> "$GITHUB_OUTPUT"', workflow)
            self.assertIn('echo "present=false" >> "$GITHUB_OUTPUT"', workflow)

        self.assertIn('run: scripts/review_gate.sh', package)
        self.assertIn('run: docs/ai-team/scripts/review_gate.sh', adopter)
        self.assertIn("if [ -x scripts/review_gate.sh ]; then", package)
        self.assertIn("if [ -x docs/ai-team/scripts/review_gate.sh ]; then", adopter)


if __name__ == "__main__":
    unittest.main()
