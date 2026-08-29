import unittest
from pathlib import Path


README = Path(__file__).parents[1] / "README.md"


class StaleLaneContractTest(unittest.TestCase):
    """The manual supersede path must retain the attribution land.sh preserves."""

    def test_superseded_close_preserves_the_agent_attribution_label(self):
        document = README.read_text(encoding="utf-8")
        section = document.split("## Stale-lane recovery", 1)[1].split("\n---", 1)[0]
        contract = " ".join(section.split())

        self.assertIn("replacement PR and merged SHA", contract)
        self.assertIn("move only its `task:*`", contract)
        self.assertIn("preserve its existing `agent:<id>` label", contract)


if __name__ == "__main__":
    unittest.main()
