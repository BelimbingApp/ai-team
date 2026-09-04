import io
import unittest
from contextlib import redirect_stdout
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from blocked_by_sweep import (
    BLOCKED_LABEL,
    READY_LABEL,
    BlockerReference,
    GitHubAPI,
    comment_marker,
    parse_blocker_references,
    sweep_slug,
    parse_blockers,
    sweep,
    transition_for,
)


class BlockedBySweepTest(unittest.TestCase):
    def test_parses_one_or_more_comma_separated_blockers(self):
        self.assertEqual(parse_blockers("Blocked-By: #131, #132, #131"), (131, 132))

    def test_rejects_missing_or_malformed_header(self):
        self.assertEqual(parse_blockers("blocked on #131"), ())
        self.assertEqual(parse_blockers("Blocked-By: #131 and #132"), ())

    def test_parses_mixed_local_and_qualified_references(self):
        self.assertEqual(
            parse_blocker_references(
                "Blocked-By: #131, BelimbingApp/blb-people-connector#97, #132."
            ),
            (
                BlockerReference(None, 131),
                BlockerReference("belimbingapp/blb-people-connector", 97),
                BlockerReference(None, 132),
            ),
        )

    def test_qualified_references_dedupe_case_insensitively(self):
        self.assertEqual(
            parse_blocker_references(
                "Blocked-By: BelimbingApp/People#7, belimbingapp/people#7"
            ),
            (BlockerReference("belimbingapp/people", 7),),
        )

    def test_original_integer_parser_fails_closed_on_qualified_references(self):
        body = "Blocked-By: #1, BelimbingApp/connector#2, #3"

        self.assertEqual(parse_blockers(body), ())
        self.assertIsNone(transition_for(
            {"body": body, "labels": [{"name": BLOCKED_LABEL}]},
            {1: "closed", 3: "closed"},
        ))

    def test_rejects_urls_and_malformed_qualified_references(self):
        self.assertEqual(
            parse_blocker_references(
                "Blocked-By: https://github.com/BelimbingApp/connector/issues/2"
            ),
            (),
        )
        self.assertEqual(parse_blocker_references("Blocked-By: owner/repo/extra#2"), ())

    def test_one_malformed_declaration_invalidates_other_valid_declarations(self):
        body = "Blocked-By: #12.\nBlocked-By: owner/repo#oops."

        self.assertEqual(parse_blocker_references(body), ())
        self.assertEqual(parse_blockers(body), ())

    def test_derives_the_marker_from_the_repository_name(self):
        # #2: no adopter ships another repository's name in its comments.
        self.assertEqual(
            comment_marker((131, 132), "BelimbingApp/ai-team"),
            "<!-- ai-team-blocked-by-sweep:131,132 -->",
        )
        self.assertEqual(
            comment_marker((131, 132), "Org/Belimbing"),
            "<!-- belimbing-blocked-by-sweep:131,132 -->",
        )

    def test_the_slug_sanitizes_and_degrades_rather_than_crashing(self):
        self.assertEqual(sweep_slug("Org/My_Repo.Name"), "my-repo-name")
        self.assertEqual(sweep_slug(None), "ai-team")
        self.assertEqual(sweep_slug("Org/___"), "ai-team")

    def test_a_marker_under_a_foreign_prefix_still_counts_as_already_posted(self):
        # #2's migration trap: adopting this package (or renaming the
        # repository) changes the marker's prefix; the sweep must keep
        # matching comments written under the old name, or it re-posts a
        # duplicate on every issue it has ever swept.
        blockers = (131, 132)
        issue = {"labels": ["task:blocked"], "body": "Blocked-By: #131, #132"}
        transition = transition_for(
            issue,
            {131: "closed", 132: "closed"},
            ["previous result <!-- bilimbi-blocked-by-sweep:131,132 -->"],
            "BelimbingApp/ai-team",
        )
        self.assertIsNotNone(transition)
        self.assertIsNone(transition.comment)
        self.assertIn("task:ready", transition.labels)

    def test_a_foreign_marker_for_different_blockers_does_not_suppress_the_comment(self):
        issue = {"labels": ["task:blocked"], "body": "Blocked-By: #131"}
        transition = transition_for(
            issue,
            {131: "closed"},
            ["<!-- bilimbi-blocked-by-sweep:7,8 -->"],
            "BelimbingApp/ai-team",
        )
        self.assertIsNotNone(transition)
        self.assertIsNotNone(transition.comment)
        self.assertIn("ai-team-blocked-by-sweep:131", transition.comment)

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
            repository = "Example/ai-team"
            def open_blocked_issues(self):
                return [{"number": 201, "pull_request": {}, "labels": []}]

        self.assertEqual(sweep(FakeAPI()), 0)

    def test_sweep_comments_and_relabels_only_after_all_blockers_close(self):
        from blocked_by_sweep import sweep

        class FakeAPI:
            repository = "Example/ai-team"
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
            repository = "Example/ai-team"
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

    def test_qualified_open_blocker_prevents_local_closed_blocker_from_unblocking(self):
        class FakeAPI:
            repository = "Example/people"

            def __init__(self):
                self.labels_written = None

            def open_blocked_issues(self):
                return [{
                    "number": 13,
                    "body": "Blocked-By: #12, Example/connector#97",
                    "labels": [{"name": BLOCKED_LABEL}],
                }]

            def issue_state(self, number):
                return "closed"

            def repository_issue_state(self, repository, number):
                self.assert_reference = (repository, number)
                return "open"

            def replace_labels(self, number, labels):
                self.labels_written = labels

        api = FakeAPI()

        self.assertEqual(sweep(api), 0)
        self.assertEqual(api.assert_reference, ("example/connector", 97))
        self.assertIsNone(api.labels_written)

    def test_unknown_qualified_blocker_fails_closed(self):
        class FakeAPI:
            repository = "Example/people"

            def open_blocked_issues(self):
                return [{
                    "number": 13,
                    "body": "Blocked-By: Example/private-connector#97",
                    "labels": [{"name": BLOCKED_LABEL}],
                }]

            def repository_issue_state(self, repository, number):
                return None

        self.assertEqual(sweep(FakeAPI()), 0)

    def test_malformed_second_declaration_causes_no_comment_or_label_write(self):
        class FakeAPI:
            repository = "Example/people"

            def __init__(self):
                self.comments_seen = []
                self.labels_written = None
                self.state_reads = []

            def open_blocked_issues(self):
                return [{
                    "number": 13,
                    "body": "Blocked-By: #12.\nBlocked-By: owner/repo#oops.",
                    "labels": [{"name": BLOCKED_LABEL}],
                }]

            def issue_state(self, number):
                self.state_reads.append(number)
                return "closed"

            def comments(self, number):
                return self.comments_seen

            def add_comment(self, number, body):
                self.comments_seen.append(body)

            def replace_labels(self, number, labels):
                self.labels_written = labels

        api = FakeAPI()

        self.assertEqual(sweep(api), 0)
        self.assertEqual(api.state_reads, [])
        self.assertEqual(api.comments_seen, [])
        self.assertIsNone(api.labels_written)

    def test_all_qualified_blockers_closed_unblocks_with_unambiguous_marker(self):
        class FakeAPI:
            repository = "Example/people"

            def __init__(self):
                self.comments_seen = []
                self.labels_written = None

            def open_blocked_issues(self):
                return [{
                    "number": 13,
                    "body": "Blocked-By: Example/one#7, Example/two#7",
                    "labels": [{"name": BLOCKED_LABEL}],
                }]

            def repository_issue_state(self, repository, number):
                return "closed"

            def comments(self, number):
                return self.comments_seen

            def add_comment(self, number, body):
                self.comments_seen.append(body)

            def replace_labels(self, number, labels):
                self.labels_written = labels

        api = FakeAPI()

        self.assertEqual(sweep(api), 1)
        self.assertEqual(api.labels_written, (READY_LABEL,))
        self.assertIn("example/one#7, example/two#7", api.comments_seen[0])
        self.assertIn(
            "people-blocked-by-sweep:example/one#7,example/two#7",
            api.comments_seen[0],
        )


class QualifiedGitHubAPITest(unittest.TestCase):
    def test_qualified_lookup_targets_the_declared_repository(self):
        api = GitHubAPI("Example/people", "secret")
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"state":"open"}'

        with patch("blocked_by_sweep.urlopen", return_value=response) as mocked:
            self.assertEqual(api.repository_issue_state("example/connector", 97), "open")

        self.assertEqual(
            mocked.call_args.args[0].full_url,
            "https://api.github.com/repos/example/connector/issues/97",
        )

    def test_missing_or_inaccessible_qualified_issue_is_unknown(self):
        api = GitHubAPI("Example/people", "secret")
        missing = HTTPError("url", 404, "not found", {}, None)

        with patch("blocked_by_sweep.urlopen", side_effect=missing):
            self.assertIsNone(api.repository_issue_state("example/private", 97))

    def test_non_not_found_api_failure_aborts_instead_of_inferring_state(self):
        api = GitHubAPI("Example/people", "secret")
        failure = HTTPError("url", 503, "unavailable", {}, None)

        with patch("blocked_by_sweep.urlopen", side_effect=failure):
            with self.assertRaises(HTTPError):
                api.repository_issue_state("example/connector", 97)


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


class HtmlCommentBoundaryTest(unittest.TestCase):
    """Markdown renders HTML comments invisible, so they are not prose (#3)."""

    def test_a_declaration_inside_a_comment_line_is_not_a_declaration(self):
        self.assertEqual(parse_blockers("<!-- Blocked-By: #1 -->"), ())

    def test_a_declaration_inside_a_multiline_comment_is_not_a_declaration(self):
        self.assertEqual(parse_blockers("<!--\nBlocked-By: #2\n-->\nBlocked-By: #3\n"), (3,))

    def test_an_inline_comment_span_is_invisible_text(self):
        self.assertEqual(parse_blockers("see <!-- Blocked-By: #4. --> nothing"), ())

    def test_prose_before_an_unclosed_comment_opener_still_declares(self):
        self.assertEqual(parse_blockers("Blocked-By: #5. <!-- note"), (5,))

    def test_a_comment_opener_inside_a_fence_does_not_open_a_comment(self):
        self.assertEqual(parse_blockers("```\n<!--\n```\nBlocked-By: #6\n"), (6,))

    def test_a_fence_marker_inside_a_comment_does_not_open_a_fence(self):
        self.assertEqual(parse_blockers("<!--\n```\n-->\nBlocked-By: #10\n"), (10,))

    def test_prose_after_a_multiline_comment_closes_on_the_same_line_declares(self):
        self.assertEqual(parse_blockers("<!--\nnoise\n--> Blocked-By: #9.\n"), (9,))

    def test_a_documented_example_declaration_in_a_comment_never_arms_the_sweep(self):
        self.assertEqual(parse_blockers("<!-- write: Blocked-By: #8. -->"), ())
