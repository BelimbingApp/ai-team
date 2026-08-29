import io
import unittest
from contextlib import redirect_stdout

from blocked_by_sweep import (
    BLOCKED_LABEL,
    READY_LABEL,
    comment_marker,
    parse_blockers,
    transition_for,
)


class BlockedBySweepTest(unittest.TestCase):
    def test_parses_one_or_more_comma_separated_blockers(self):
        self.assertEqual(parse_blockers("Blocked-By: #131, #132, #131"), (131, 132))

    def test_rejects_missing_or_malformed_header(self):
        self.assertEqual(parse_blockers("blocked on #131"), ())
        self.assertEqual(parse_blockers("Blocked-By: #131 and #132"), ())

    def test_uses_a_belimbing_specific_idempotency_marker(self):
        self.assertEqual(
            comment_marker((131, 132)),
            "<!-- belimbing-blocked-by-sweep:131,132 -->",
        )

    def test_leaves_open_blockers_untouched(self):
        issue = {"body": "Blocked-By: #131", "labels": [{"name": BLOCKED_LABEL}]}
        self.assertIsNone(transition_for(issue, {131: "open"}))

    def test_marks_ready_and_comments_when_all_blockers_are_closed(self):
        issue = {
            "body": "Blocked-By: #131, #132",
            "labels": [{"name": BLOCKED_LABEL}, {"name": "stage:S3"}],
        }
        transition = transition_for(issue, {131: "closed", 132: "closed"}, [])

        self.assertEqual(transition.labels, ("stage:S3", READY_LABEL))
        self.assertIn("#131, #132", transition.comment)

    def test_existing_sweep_comment_makes_transition_idempotent(self):
        blockers = (131, 132)
        issue = {
            "body": "Blocked-By: #131, #132",
            "labels": [{"name": BLOCKED_LABEL}],
        }
        transition = transition_for(
            issue,
            {131: "closed", 132: "closed"},
            [f"previous result {comment_marker(blockers)}"],
        )

        self.assertEqual(transition.labels, (READY_LABEL,))
        self.assertIsNone(transition.comment)

    def test_pull_request_objects_are_not_swept(self):
        from blocked_by_sweep import sweep

        class FakeAPI:
            def open_blocked_issues(self):
                return [{"number": 201, "pull_request": {}, "labels": []}]

        self.assertEqual(sweep(FakeAPI()), 0)

    def test_sweep_comments_and_relabels_only_after_all_blockers_close(self):
        from blocked_by_sweep import sweep

        class FakeAPI:
            def __init__(self):
                self.issue = {
                    "number": 199,
                    "body": "Blocked-By: #131, #132",
                    "labels": [{"name": BLOCKED_LABEL}],
                }
                self.comments_seen = []
                self.labels_written = None

            def open_blocked_issues(self):
                return [self.issue]

            def issue_state(self, number):
                return {131: "closed", 132: "closed"}[number]

            def comments(self, number):
                return self.comments_seen

            def add_comment(self, number, body):
                self.comments_seen.append(body)

            def replace_labels(self, number, labels):
                self.labels_written = labels

        api = FakeAPI()

        self.assertEqual(sweep(api), 1)
        self.assertEqual(api.labels_written, (READY_LABEL,))
        self.assertIn("#131, #132", api.comments_seen[0])

    def test_sweep_replays_issue_345s_inline_blocker_and_prints_the_transition(self):
        from blocked_by_sweep import sweep

        class FakeAPI:
            def __init__(self):
                self.issue = {
                    "number": 345,
                    "body": "Parent: #339. Blocked-By: #344. Blocks lane 3 (sync preparation and publication) — **lane 3 must not merge before this does.**",
                    "labels": [{"name": BLOCKED_LABEL}],
                }
                self.labels_written = None

            def open_blocked_issues(self):
                return [self.issue]

            def issue_state(self, number):
                return {344: "closed"}[number]

            def comments(self, number):
                return []

            def add_comment(self, number, body):
                pass

            def replace_labels(self, number, labels):
                self.labels_written = labels

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(sweep(FakeAPI()), 1)

        self.assertEqual(output.getvalue(), "unblocked issue #345\n")


class BlockedByParsingTest(unittest.TestCase):
    """Fail-open cases found while reviewing #204, fixed in #224.

    Each of these previously unblocked an issue it should not have.
    """

    def blocked(self, body: str) -> dict:
        return {"body": body, "labels": [{"name": BLOCKED_LABEL}]}

    def test_every_header_counts_not_only_the_first(self):
        # Recording a new blocker by adding a line is the natural edit. Reading
        # only the first header dropped the rest and marked the issue ready
        # while #2 was still open.
        body = "Blocked-By: #1\n\nlater:\nBlocked-By: #2\n"

        self.assertEqual(parse_blockers(body), (1, 2))
        self.assertIsNone(transition_for(self.blocked(body), {1: "closed", 2: "open"}, []))

    def test_all_headers_closed_still_unblocks(self):
        body = "Blocked-By: #1\nBlocked-By: #2\n"

        transition = transition_for(self.blocked(body), {1: "closed", 2: "closed"}, [])

        self.assertIsNotNone(transition)
        self.assertIn(READY_LABEL, transition.labels)

    def test_parses_issue_345s_verbatim_inline_declaration(self):
        body = """## Problem

Lane 2 of #339, split out per the decomposition on that issue.

#339 requires that upstream synchronization be available **only** in an explicitly enabled development or staging administration environment, and that production remain a read-only deployment consumer. That boundary is the safety property of the whole feature, and it must exist *before* anything can act on it.

## Scope

The gate, and only the gate. No synchronization logic, no write credentials, no PR creation — those are lane 3.

- An explicit deployment-role / feature setting that enables synchronization capability. Off by default; production cannot enable it by accident or by inheriting a development config.
- An authorization capability governing who may use it, consistent with how this codebase already gates admin actions.
- Read-only upstream visibility (#344) stays available regardless of the gate — seeing is not synchronizing.
- When the gate is closed, the UI states plainly that synchronization is unavailable and why, rather than hiding the concept or failing at the point of use.

## Required resolution

Add the setting and capability, and make them the precondition every later synchronization action checks. Fail closed: an unset or unrecognised deployment role means synchronization is unavailable.

Gate at the server, not only in the view. A hidden button is not a boundary — #307 in this codebase is the precedent for a UI-level assumption that broke at the server.

## Acceptance

- Synchronization capability is unavailable in production and unavailable by default anywhere.
- Enabling it requires an explicit, deliberate setting plus the authorization capability.
- Read-only upstream visibility is unaffected by the gate's state.
- A closed gate produces an explanatory state, not a silent absence or a runtime failure.
- Focused tests cover: production, unset role, development-without-capability, and development-with-capability.
- Relevant tests, Pint, and diff-check pass.

Parent: #339. Blocked-By: #344. Blocks lane 3 (sync preparation and publication) — **lane 3 must not merge before this does.**
"""

        self.assertEqual(parse_blockers(body), (344,))

    def test_header_inside_a_fenced_block_is_not_a_declaration(self):
        # Documenting the convention in an issue body must not arm the sweep
        # against that issue.
        body = "Syntax:\n```\nBlocked-By: #1\n```\n"

        self.assertEqual(parse_blockers(body), ())

    def test_header_inside_an_indented_block_is_not_a_declaration(self):
        self.assertEqual(parse_blockers("Example:\n\n    Blocked-By: #1\n"), ())

    def test_spaces_then_a_tab_are_still_indented_code(self):
        # Found by cursor/auto on #225. `^(?: {4}|\t)` missed one to three
        # spaces followed by a tab, which Markdown reads as code.
        self.assertEqual(parse_blockers("Example:\n\n \tBlocked-By: #7\n"), ())
        self.assertEqual(parse_blockers("   \tBlocked-By: #7\n"), ())

    def test_an_indented_fence_does_not_close_a_block(self):
        # Also cursor/auto on #225. Markdown reads an indented ``` as code, not
        # as a closer -- closing there treats the rest of the body as prose.
        self.assertEqual(parse_blockers("```\n    ```\nBlocked-By: #9\n"), ())

    def test_a_real_closer_after_an_indented_one_still_closes(self):
        # The guard must not swallow the body forever.
        self.assertEqual(parse_blockers("```\n    ```\n```\nBlocked-By: #1\n"), (1,))

    def test_a_longer_fence_is_not_closed_by_a_shorter_one(self):
        # ```` opens; ``` does not close it, so the header stays fenced.
        self.assertEqual(parse_blockers("````\nBlocked-By: #1\n```\n"), ())

    def test_tildes_and_backticks_do_not_close_each_other(self):
        self.assertEqual(parse_blockers("~~~\nBlocked-By: #1\n```\n"), ())

    def test_a_quoted_header_is_not_a_declaration(self):
        self.assertEqual(parse_blockers("> Blocked-By: #1\n"), ())

    def test_an_inline_declaration_inside_a_fenced_block_is_not_a_declaration(self):
        self.assertEqual(parse_blockers("```\nParent: #1. Blocked-By: #2.\n```\n"), ())

    def test_a_header_after_a_closed_fence_still_counts(self):
        # The guard must not swallow the rest of the body.
        self.assertEqual(parse_blockers("```\nnoise\n```\nBlocked-By: #1\n"), (1,))


if __name__ == "__main__":
    unittest.main()
