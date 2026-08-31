import json
import os
import tempfile
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


SCRIPT = Path(__file__).with_name("review_gate.sh")
SHA = "a" * 40
STALE_SHA = "b" * 40


class ReviewGateTest(unittest.TestCase):
    def run_gate(self, reviews, labels=("agent:author",), reviewed=SHA):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "review.json"
            fixture.write_text(
                json.dumps({"reviewed": reviewed, "labels": list(labels), "reviews": reviews}),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["REVIEW_GATE_INPUT"] = str(fixture)
            return run_with_bash_path(
                ["bash", bash_path(SCRIPT)],
                stub_directory=Path(directory),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def review(self, agent="reviewer", state="COMMENTED", body=None, commit_id=SHA, at="2026-01-01T00:00:00Z"):
        if body is None:
            body = f"**From:** {agent}\n\n**Verdict:** accept"
        return {
            "id": 1,
            "state": state,
            "body": body,
            "commit_id": commit_id,
            "submitted_at": at,
        }

    def test_commented_exact_head_acceptance_passes(self):
        result = self.run_gate([self.review()])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)

    def test_native_approval_still_requires_a_from_marker(self):
        result = self.run_gate([
            self.review(state="APPROVED", body="**From:** reviewer"),
        ])

        self.assertEqual(result.returncode, 0, result.stderr)

        missing_marker = self.run_gate([
            self.review(state="APPROVED", body="approved"),
        ])
        self.assertEqual(missing_marker.returncode, 1)
        self.assertIn("no independent exact-head acceptance", missing_marker.stdout)

    def test_stale_review_is_not_an_acceptance(self):
        result = self.run_gate([self.review(commit_id=STALE_SHA)])

        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_author_cannot_accept_their_own_lane(self):
        result = self.run_gate([self.review(agent="author")])

        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_latest_changes_required_supersedes_acceptance(self):
        result = self.run_gate([
            self.review(at="2026-01-01T00:00:00Z"),
            self.review(
                state="COMMENTED",
                body="**From:** reviewer\n\n**Verdict:** changes required",
                at="2026-01-01T00:01:00Z",
            ),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("changes required by reviewer", result.stdout)

    def test_comment_style_verdict_is_rejected(self):
        result = self.run_gate([
            self.review(body="**From:** reviewer\n\nVerdict: accept"),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("rejected for format", result.stdout)


if __name__ == "__main__":
    unittest.main()
