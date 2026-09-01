import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, _bash_executable, _git_tool_executable, run_with_bash_path


SCRIPT = Path(__file__).with_name("board.sh")

# gate.sh's own From-marker contract (gate.sh:~203). post output must satisfy
# it, or board.sh would mint posts the gate cannot attribute.
GATE_FROM_REGEX = re.compile(
    r"^\*\*From:\*\*\s*(?P<agent>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:\s|$)", re.IGNORECASE
)


def gh_capture_stub(directory: Path, comments_json: str = "[]") -> Path:
    """A gh stub that records `issue comment` bodies and serves fixture JSON."""
    gh = directory / "gh"
    gh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [ "$1" = "issue" ] && [ "$2" = "comment" ]; then
              cat > "$BOARD_TEST_CAPTURE"
              exit 0
            fi
            if [ "$1" = "issue" ] && [ "$2" = "view" ]; then
              cat "$BOARD_TEST_FIXTURE"
              exit $?
            fi
            if [ "$1" = "api" ]; then
              if [ -n "${BOARD_TEST_REVIEWS:-}" ] && [ -f "$BOARD_TEST_REVIEWS" ]; then
                cat "$BOARD_TEST_REVIEWS"
                exit 0
              fi
              echo "gh: Not Found (HTTP 404)" >&2
              exit 1
            fi
            if { [ "$1" = "pr" ] || [ "$1" = "issue" ]; } && [ "$2" = "list" ]; then
              input="${BOARD_TEST_LIST:-[]}"
              jq_expr=""
              shift 2
              while [ $# -gt 0 ]; do
                case "$1" in
                  --jq) jq_expr="$2"; shift 2 ;;
                  --json|--state|--label|--repo|--limit) shift 2 ;;
                  -*) shift ;;
                  *) shift ;;
                esac
              done
              if [ -n "$jq_expr" ] && [[ "$input" == \[* ]]; then
                printf '%s' "$input" | jq -r "$jq_expr"
              else
                printf '%s' "$input"
              fi
              exit 0
            fi
            exit 1
            """
        ),
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return gh


class BoardMechanismTest(unittest.TestCase):
    def run_board(self, args, directory: Path, env_extra=None, stdin: str = ""):
        env = os.environ.copy()
        env["BOARD_TEST_CAPTURE"] = bash_path(directory / "captured-body")
        env["BOARD_TEST_FIXTURE"] = bash_path(directory / "fixture.json")
        env.update(env_extra or {})
        return run_with_bash_path(
            ["bash", bash_path(SCRIPT), *args],
            stub_directory=directory,
            env=env,
            text=True,
            input=stdin,
            capture_output=True,
            check=False,
        )

    # ---- post ----

    def test_post_stamps_the_header_gate_parses(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "status", "head is abc1234"],
                directory,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (directory / "captured-body").read_text(encoding="utf-8")
            first_line = body.splitlines()[0]
            match = GATE_FROM_REGEX.match(first_line)
            self.assertIsNotNone(match, f"gate cannot parse: {first_line!r}")
            self.assertEqual(match.group("agent"), "fable")
            self.assertIn("**Type:** status", body)
            self.assertIn("head is abc1234", body)

    def test_post_folds_overflow_into_details_at_a_line_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            body_in = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(60))
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "finding"],
                directory,
                env_extra={"BOARD_POST_BUDGET": "400"},
                stdin=body_in,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (directory / "captured-body").read_text(encoding="utf-8")
            visible = body.split("<details>")[0]
            self.assertLessEqual(
                len(visible.encode()), 400 + 120, "visible part exceeds budget plus header"
            )
            self.assertIn("<details>", body)
            self.assertIn("line 059", body, "folded remainder must survive inside the details")
            # Nothing is lost at the fold boundary: every input line is present.
            for i in range(60):
                self.assertIn(f"line {i:03d}", body)

    def test_post_refuses_verdicts_and_points_at_pr_review(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "verdict", "accept"],
                directory,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("invisible to gate.sh", result.stderr)
            self.assertIn("gh pr review", result.stderr)
            self.assertIn(
                "reviewed_head=$(gh pr view 42 --json headRefOid --jq .headRefOid)",
                result.stderr,
            )
            self.assertIn("**HEAD reviewed:** %s", result.stderr)
            self.assertIn('"$reviewed_head"', result.stderr)
            self.assertFalse((directory / "captured-body").exists(), "nothing may be posted")

    def test_post_requires_an_agent_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            env = {k: v for k, v in os.environ.items()}
            env.pop("CLAIM_AGENT", None)
            env.pop("BOARD_AGENT", None)
            result = run_with_bash_path(
                ["bash", bash_path(SCRIPT), "post", "42", "--type", "status", "hello"],
                stub_directory=directory,
                env={**env, "BOARD_TEST_CAPTURE": bash_path(directory / "captured-body"),
                     "BOARD_TEST_FIXTURE": bash_path(directory / "fixture.json")},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("agent id required", result.stderr)

    def test_post_refuses_impersonating_steward_appointee(self):
        # #51: ops:steward appointment names an owner; a substitute must not
        # stamp **From:** as the appointee when CLAIM_AGENT is someone else.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "status", "queue drained"],
                directory,
                env_extra={
                    "CLAIM_AGENT": "composer-2.5",
                    "BOARD_TEST_STEWARD_APPOINTEE": "fable",
                    "BOARD_TEST_STEWARD_ISSUE": "468",
                },
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("refusing", result.stderr)
            self.assertIn("steward-backstop", result.stderr)
            self.assertFalse((directory / "captured-body").exists())

    def test_post_allows_appointee_without_claim_agent(self):
        # #51 review: the appointee may post as themselves without CLAIM_AGENT set.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            env = {k: v for k, v in os.environ.items()}
            env.pop("CLAIM_AGENT", None)
            env.pop("BOARD_AGENT", None)
            env["BOARD_TEST_CAPTURE"] = bash_path(directory / "captured-body")
            env["BOARD_TEST_FIXTURE"] = bash_path(directory / "fixture.json")
            env["BOARD_TEST_STEWARD_APPOINTEE"] = "fable"
            env["BOARD_TEST_STEWARD_ISSUE"] = "468"
            result = run_with_bash_path(
                ["bash", bash_path(SCRIPT), "post", "42", "--agent", "fable", "--type", "status", "hello"],
                stub_directory=directory,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (directory / "captured-body").read_text(encoding="utf-8")
            self.assertRegex(body.splitlines()[0], r"^\*\*From:\*\* fable")

    def test_steward_appointment_resolves_from_issue_list(self):
        # #51 review: drive the live gh issue-list lookup, not BOARD_TEST_* hooks.
        steward_issues = json.dumps([
            {
                "number": 468,
                "labels": [{"name": "ops:steward"}, {"name": "agent:fable"}],
            }
        ])
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "status", "via list lookup"],
                directory,
                env_extra={
                    "CLAIM_AGENT": "composer-2.5",
                    "BOARD_REPO": "example/canonical",
                    "BOARD_TEST_LIST": steward_issues,
                },
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("refusing", result.stderr)
            self.assertIn("steward-backstop", result.stderr)
            self.assertFalse((directory / "captured-body").exists())

    def test_steward_appointment_from_issue_list_allows_appointee(self):
        steward_issues = json.dumps([
            {
                "number": 468,
                "labels": [{"name": "ops:steward"}, {"name": "agent:fable"}],
            }
        ])
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "status", "appointee post"],
                directory,
                env_extra={
                    "BOARD_REPO": "example/canonical",
                    "BOARD_TEST_LIST": steward_issues,
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (directory / "captured-body").read_text(encoding="utf-8")
            self.assertRegex(body.splitlines()[0], r"^\*\*From:\*\* fable")

    def test_post_steward_appointment_empty_when_ambiguous(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                [
                    "post", "42",
                    "--agent", "composer-2.5",
                    "--steward-for", "sol",
                    "--type", "steward-backstop",
                    "wrong appointee",
                ],
                directory,
                env_extra={
                    "CLAIM_AGENT": "composer-2.5",
                    "BOARD_TEST_STEWARD_APPOINTEE": "fable",
                    "BOARD_TEST_STEWARD_ISSUE": "468",
                    "BOARD_TEST_STEWARD_AMBIGUOUS": "1",
                },
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("does not match active appointee", result.stderr)

    def test_post_steward_backstop_stamps_steward_for_header(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                [
                    "post", "42",
                    "--agent", "composer-2.5",
                    "--steward-for", "fable",
                    "--type", "steward-backstop",
                    "drained #457 on fable's appointment",
                ],
                directory,
                env_extra={
                    "CLAIM_AGENT": "composer-2.5",
                    "BOARD_TEST_STEWARD_APPOINTEE": "fable",
                    "BOARD_TEST_STEWARD_ISSUE": "468",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (directory / "captured-body").read_text(encoding="utf-8")
            match = GATE_FROM_REGEX.match(body.splitlines()[0])
            self.assertIsNotNone(match)
            self.assertEqual(match.group("agent"), "composer-2.5")
            self.assertIn("**Steward-for:** fable (#468)", body)
            self.assertIn("**Type:** steward-backstop", body)
            self.assertIn("drained #457 on fable's appointment", body)

    def test_post_steward_backstop_requires_steward_for_flag(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                [
                    "post", "42",
                    "--agent", "composer-2.5",
                    "--type", "steward-backstop",
                    "missing --steward-for",
                ],
                directory,
                env_extra={"CLAIM_AGENT": "composer-2.5"},
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("requires --steward-for", result.stderr)

    def test_post_allows_appointee_when_acting_as_self(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "status", "heartbeat ok"],
                directory,
                env_extra={
                    "CLAIM_AGENT": "fable",
                    "BOARD_TEST_STEWARD_APPOINTEE": "fable",
                    "BOARD_TEST_STEWARD_ISSUE": "468",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body = (directory / "captured-body").read_text(encoding="utf-8")
            self.assertRegex(body.splitlines()[0], r"^\*\*From:\*\* fable")

    def test_post_refuses_steward_for_mismatch(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                [
                    "post", "42",
                    "--agent", "composer-2.5",
                    "--steward-for", "sol",
                    "--type", "steward-backstop",
                    "wrong appointee",
                ],
                directory,
                env_extra={
                    "CLAIM_AGENT": "composer-2.5",
                    "BOARD_TEST_STEWARD_APPOINTEE": "fable",
                    "BOARD_TEST_STEWARD_ISSUE": "468",
                },
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("does not match active appointee fable", result.stderr)

    # ---- digest ----

    def digest_fixture(self) -> str:
        structured = (
            "**From:** sol\n\n**Type:** finding\n\n"
            + "the lease is load-bearing\n"
            + "<details>\n<summary>evidence</summary>\n\nreams of git output\n\n</details>\n"
            + "\n".join(f"detail line {i}" for i in range(20))
        )
        return json.dumps(
            {
                "number": 42,
                "title": "Example lane",
                "state": "OPEN",
                "labels": [{"name": "task:review"}, {"name": "agent:fable"}],
                "comments": [
                    {"body": structured, "createdAt": "2026-08-27T04:00:00Z",
                     "author": {"login": "kiatng"}},
                    {"body": "Owner response: fork is not involved", "createdAt": "2026-08-27T04:01:00Z",
                     "author": {"login": "kiatng"}},
                    {"body": "## Quality Gate Passed\nbot noise", "createdAt": "2026-08-27T04:02:00Z",
                     "author": {"login": "sonarqubecloud"}},
                ],
            }
        )

    def test_digest_renders_structured_posts_and_counts_noise(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            (directory / "fixture.json").write_text(self.digest_fixture(), encoding="utf-8")
            result = self.run_board(["digest", "42"], directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("== #42 [OPEN] Example lane", result.stdout)
            self.assertIn("task:review", result.stdout)
            self.assertIn("-- sol", result.stdout)
            self.assertIn("the lease is load-bearing", result.stdout)
            # P1 (#364): an unheadered post from a human account may be the
            # owner, whose rulings outrank every marker — rendered, never hidden.
            self.assertIn("[no header] kiatng", result.stdout)
            self.assertIn("Owner response: fork is not involved", result.stdout)
            # Bot posts are ignored with a count, not rendered and not nagged.
            self.assertIn("1 bot post(s) ignored", result.stdout)
            self.assertNotIn("Quality Gate Passed", result.stdout)

    def test_digest_recognises_a_from_marker_after_preamble(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            fixture = json.dumps(
                {
                    "number": 42,
                    "title": "Preamble lane",
                    "state": "OPEN",
                    "labels": [{"name": "task:review"}],
                    "comments": [
                        {
                            "body": (
                                "Context before the marker.\n\n"
                                "**From:** sonnet-5\n\n"
                                "**Type:** finding\n\nDetails here."
                            ),
                            "createdAt": "2026-08-28T06:00:00Z",
                            "author": {"login": "kiatng"},
                        }
                    ],
                }
            )
            (directory / "fixture.json").write_text(fixture, encoding="utf-8")

            result = self.run_board(["digest", "42"], directory)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("-- sonnet-5", result.stdout)
            self.assertNotIn("[no header]", result.stdout)
            self.assertIn("Context before the marker.", result.stdout)
            self.assertIn("Details here.", result.stdout)
            self.assertNotIn("**From:** sonnet-5", result.stdout)

    def test_digest_strips_folded_details_and_truncates_long_posts(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            (directory / "fixture.json").write_text(self.digest_fixture(), encoding="utf-8")
            result = self.run_board(
                ["digest", "42"], directory, env_extra={"BOARD_DIGEST_LINES": "5"}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[folded detail omitted]", result.stdout)
            self.assertNotIn("reams of git output", result.stdout)
            self.assertIn("more lines — read the thread only if you need them", result.stdout)
            self.assertNotIn("detail line 19", result.stdout)

    def test_post_budget_cut_inside_a_multibyte_character_stays_valid_utf8(self):
        # P3 (#364): a single long line with no newline inside the budget used
        # to be cut mid-character by head -c. The visible part must decode as
        # UTF-8 and every input character must survive somewhere in the post.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            body_in = "\u00e9" * 300  # 600 bytes of two-byte characters, no newline
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "finding"],
                directory,
                env_extra={"BOARD_POST_BUDGET": "201"},
                stdin=body_in,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            raw_bytes = (directory / "captured-body").read_bytes()
            decoded = raw_bytes.decode("utf-8")  # raises on a split character
            self.assertEqual(decoded.count("\u00e9"), 300, "no character may be lost")

    def test_digest_merges_the_pr_review_stream_where_verdicts_live(self):
        # P1a (#364): verdicts are reviews, not conversation comments — post
        # itself redirects verdict-writers there — so a digest reading only
        # issue comments hides every verdict, including a blocking one.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            (directory / "fixture.json").write_text(self.digest_fixture(), encoding="utf-8")
            reviews = json.dumps(
                [
                    {
                        "body": "**From:** sol\n\n**Verdict:** changes required\n\nthe reader reads the wrong stream",
                        "submitted_at": "2026-08-27T05:00:00Z",
                        "state": "COMMENTED",
                        "commit_id": "489f958363cc5e04cd1881eddeadbeefdeadbeef",
                        "user": {"login": "faith-tohmm"},
                    }
                ]
            )
            (directory / "reviews.json").write_text(reviews, encoding="utf-8")
            result = self.run_board(
                ["digest", "42"], directory,
                env_extra={"BOARD_TEST_REVIEWS": bash_path(directory / "reviews.json")},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[review COMMENTED @489f958]", result.stdout)
            self.assertIn("**Verdict:** changes required", result.stdout)
            self.assertIn("the reader reads the wrong stream", result.stdout)

    def test_digest_of_a_plain_issue_survives_the_reviews_endpoint_404(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            (directory / "fixture.json").write_text(self.digest_fixture(), encoding="utf-8")
            result = self.run_board(["digest", "42"], directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("-- sol", result.stdout)

    def test_digest_fails_loudly_when_the_read_itself_fails(self):
        # sol's pipefail finding: a failed gh read must not hand jq empty
        # input and exit 0 as an empty-looking digest.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)  # fixture.json deliberately absent
            result = self.run_board(["digest", "42"], directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not read", result.stderr)

    def test_post_budget_smaller_than_one_character_keeps_every_byte_in_the_fold(self):
        # sol's P2 (#364): iconv correctly emits NOTHING for a one-byte prefix
        # of a two-byte character; treating empty output as failure restored
        # the invalid byte — the exit-status error repeated one layer down.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            result = self.run_board(
                ["post", "42", "--agent", "fable", "--type", "finding"],
                directory,
                env_extra={"BOARD_POST_BUDGET": "1"},
                stdin="\u00e9",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            raw_bytes = (directory / "captured-body").read_bytes()
            decoded = raw_bytes.decode("utf-8")
            self.assertEqual(decoded.count("\u00e9"), 1)

    def test_post_without_iconv_falls_back_to_the_raw_cut_not_an_empty_post(self):
        # #369: on a box without iconv the previous behavior published an
        # EMPTY visible section for every over-budget single-line post. The
        # lesser harm is one possibly-split trailing character; the visible
        # section must stay non-empty.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            # A PATH that genuinely lacks iconv: stage only what post needs.
            # Copying instead of symlinking keeps this fixture usable on
            # ordinary Windows checkouts without SeCreateSymbolicLinkPrivilege.
            # Resolve Bash through the shared helper so ordinary PowerShell
            # still finds Git-for-Windows even when bash.exe is not itself on
            # PATH (#389).
            bash = _bash_executable()
            for tool in ("bash", "head", "tail", "wc", "cat", "sed"):
                real = (
                    bash
                    if tool == "bash"
                    else shutil.which(tool) or _git_tool_executable(tool)
                )
                self.assertIsNotNone(real, f"{tool} required for the fixture")
                destination_name = Path(real).name if os.name == "nt" else tool
                destination = directory / destination_name
                shutil.copy2(real, destination)
                if os.name != "nt":
                    destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["BOARD_TEST_CAPTURE"] = bash_path(directory / "captured-body")
            env["BOARD_TEST_FIXTURE"] = bash_path(directory / "fixture.json")
            env["BOARD_POST_BUDGET"] = "201"
            # PATH is deliberately reduced to the fixture's tools, so neither gh
            # nor git is reachable to name the repository. board.sh no longer
            # assumes one (#445), so state it explicitly — this test is about
            # the iconv fallback, not about repository resolution.
            env["BOARD_REPO"] = "example/canonical"
            env["PATH"] = bash_path(directory)
            result = subprocess.run(
                [bash, bash_path(SCRIPT), "post", "42",
                 "--agent", "fable", "--type", "finding"],
                input="\u00e9" * 300,
                env=env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            body_bytes = (directory / "captured-body").read_bytes()
            visible = body_bytes.split(b"<details>")[0]
            # The visible section carries real content (header ~40 bytes plus
            # ~200 payload bytes), not just the header over an empty body.
            self.assertGreater(len(visible), 150, "visible section must not be empty")
            # Byte conservation still holds: every input byte is in the post.
            # The raw-cut fallback may split one pair on POSIX (299 complete
            # pairs), while Git-for-Windows can land on the pair boundary
            # (300 complete pairs). Both outcomes preserve all input bytes.
            self.assertEqual(body_bytes.count(b"\xc3"), 300)
            self.assertEqual(body_bytes.count(b"\xa9"), 300)
            self.assertGreaterEqual(body_bytes.count(b"\xc3\xa9"), 299)

    # ---- hygiene ----

    def test_hygiene_counts_unstructured_posts_on_active_lanes(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            (directory / "fixture.json").write_text(self.digest_fixture(), encoding="utf-8")
            result = self.run_board(
                ["hygiene"], directory, env_extra={"BOARD_TEST_LIST": "42\n"}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            # P2 (#364): the bot comment is excluded — only the human-account
            # unheadered post counts, since only that one could have been an
            # agent posting correctly.
            self.assertIn("#42 has 1 unstructured post(s)", result.stdout)

    def test_hygiene_reports_clean_when_every_post_is_structured(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            gh_capture_stub(directory)
            clean = json.dumps(
                {
                    "number": 7,
                    "title": "Clean lane",
                    "state": "OPEN",
                    "labels": [],
                    "comments": [
                        {"body": "Context first.\n\n**From:** fable\n\n**Type:** status\n\nok", "createdAt": "x",
                         "author": {"login": "kiatng"}},
                        {"body": "## Quality Gate Passed", "createdAt": "x",
                         "author": {"login": "sonarqubecloud"}},
                    ],
                }
            )
            (directory / "fixture.json").write_text(clean, encoding="utf-8")
            result = self.run_board(
                ["hygiene"], directory, env_extra={"BOARD_TEST_LIST": "7\n"}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ok      every post on active lanes carries the machine header", result.stdout)


if __name__ == "__main__":
    unittest.main()
