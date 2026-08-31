import unittest
from pathlib import Path

README = Path(__file__).parents[1] / "README.md"


class AutonomousDeliberationContractTest(unittest.TestCase):
    """#430: routine owner-question blocking must be gone from the charter,
    and the deliberation mechanism it was replaced with must be documented."""

    def test_no_owner_decision_queue_or_pause_for_owner_language_remains(self):
        document = README.read_text(encoding="utf-8")
        lowered = document.lower()
        for stale_phrase in (
            "owner-decision queue",
            "queue designated by the owner",
            "surfaces owner-only decisions",
        ):
            self.assertNotIn(
                stale_phrase.lower(), lowered,
                f"stale owner-decision-queue language survived: {stale_phrase!r}",
            )

    def test_the_autonomous_deliberation_section_documents_decide_sh(self):
        document = README.read_text(encoding="utf-8")
        self.assertIn("## Autonomous deliberation", document)
        section = document.split("## Autonomous deliberation", 1)[1].split("\n---", 1)[0]
        contract = " ".join(section.split())

        for required in (
            "decide.sh propose",
            "decide.sh vote",
            "decide.sh close",
            "authority stack",
            "quorum",
            "tie-break",
            "external-authority",
        ):
            self.assertIn(required, contract)

    def test_the_self_interest_tie_break_carve_out_is_documented(self):
        document = README.read_text(encoding="utf-8")
        section = document.split("## Autonomous deliberation", 1)[1].split("\n---", 1)[0]
        contract = " ".join(section.split())
        self.assertIn("expand, waive, or transfer the steward's own authority", contract)
        self.assertIn("--authority-effect", contract)

    def test_the_scoped_owner_delegation_clause_is_documented(self):
        document = README.read_text(encoding="utf-8")
        section = document.split("## Autonomous deliberation", 1)[1].split("\n---", 1)[0]
        contract = " ".join(section.split())
        self.assertIn("--owner-delegation", contract)
        self.assertIn("never generalized", contract)
        self.assertIn("silence", contract)

    def test_did_not_vote_and_unacknowledged_are_documented_as_distinct(self):
        document = README.read_text(encoding="utf-8")
        section = document.split("## Autonomous deliberation", 1)[1].split("\n---", 1)[0]
        contract = " ".join(section.split())
        self.assertIn("**Did-Not-Vote:**", contract)
        self.assertIn("**Unacknowledged:**", contract)
        self.assertIn("decide.sh notify", contract)

    def test_the_where_things_live_table_no_longer_points_at_an_owner_queue(self):
        document = README.read_text(encoding="utf-8")
        section = document.split("## Where things live", 1)[1].split("\n---", 1)[0]
        self.assertNotIn("Owner decisions", section)
        self.assertIn("decide.sh propose", section)


if __name__ == "__main__":
    unittest.main()
