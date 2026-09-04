import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path

SCRIPT = Path(__file__).with_name("claim.sh")
ORIENT = Path(__file__).with_name("orient.sh")


class ClaimOwnLabelTest(unittest.TestCase):
    """#366: an agent's own label is a resume, not a collision.

    Three manual claims in one mission — each forced by claim.sh refusing the
    claimant's own label on a self-filed follow-up — silently skipped every
    invariant the script guarantees, and the first signal was a gate failure
    at merge time.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        env = self.git_env()
        self.bare = base / "canonical.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        subprocess.run(
            ["git", "--git-dir", str(self.bare), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True, env=env,
        )
        seed = base / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=env)
        (seed / "README").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=seed, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=seed, check=True, env=env)
        subprocess.run(["git", "remote", "add", "origin", str(self.bare)], cwd=seed, check=True, env=env)
        subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=seed, check=True, env=env)

        self.clone = base / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.clone)], check=True, env=env)

        self.bin = base / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                case "$1 $2" in
                  "repo view") printf 'example/canonical\\n' ;;
                  "issue list") ;;
                  "issue view")
                    # claim.sh reads the labels back after writing them and
                    # judges the lookup by its exit status, so this has to
                    # answer --json labels --jq the way gh does (#15).
                    if printf '%s' "$*" | grep -q -- '--json labels'; then
                      printf 'agent:%s,task:active\\n' "${CLAIM_AGENT:-}"
                    else
                      printf '%s\\n' "$CLAIM_TEST_ISSUE_JSON"
                    fi
                    ;;
                  "pr view") printf 'agent:%s,task:active\\n' "${CLAIM_AGENT:-}" ;;
                  "pr list") printf '%s\\n' "${CLAIM_TEST_PR_LIST:-[]}" ;;
                  "label list") printf '[{"name":"agent:fable"}]\\n' ;;
                  "pr create")
                    prev=""
                    for arg in "$@"; do
                      if [ "$prev" = "--body-file" ] && [ -f "$arg" ]; then
                        cp "$arg" "${CLAIM_TEST_BODY_CAPTURE:-/dev/null}"
                      fi
                      prev="$arg"
                    done
                    printf 'https://example/pull/99\\n'
                    ;;
                  "pr edit") exit 0 ;;
                  "issue edit")
                    if printf '%s' "$*" | grep -q -- '--remove-label task:ready'; then
                      exit "${CLAIM_TEST_REMOVE_READY_EXIT:-0}"
                    fi
                    exit 0
                    ;;
                  *) exit 1 ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.dir.cleanup()

    def git_env(self):
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                "AI_TEAM_TEST_ORIGIN_REPO": "example/canonical",
            }
        )
        return env

    def run_claim(self, issue_json: dict, agent: str = "fable", pr_list: str = "[]",
                  remove_ready_exit: str = "0"):
        env = self.git_env()
        env["CLAIM_AGENT"] = agent
        env["CLAIM_TEST_ISSUE_JSON"] = json.dumps(issue_json)
        env["CLAIM_TEST_PR_LIST"] = pr_list
        env["CLAIM_TEST_REMOVE_READY_EXIT"] = remove_ready_exit
        env["CLAIM_TEST_BODY_CAPTURE"] = bash_path(self.bin / "captured-pr-body")
        return run_with_bash_path(
            ["bash", bash_path(SCRIPT), "42"],
            stub_directory=self.bin,
            env=env,
            cwd=self.clone,
            text=True,
            capture_output=True,
            check=False,
        )

    def issue(self, labels):
        return {
            "state": "OPEN",
            "labels": [{"name": name} for name in labels],
            "title": "self-filed follow-up",
            "url": "https://example/issues/42",
        }

    def test_own_label_without_task_ready_resumes_and_claims(self):
        result = self.run_claim(self.issue(["agent:fable"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resuming #42: it already carries your own label", result.stdout)
        self.assertIn("claimed #42 in draft PR", result.stdout)

    def test_existing_label_skips_label_create_and_claims(self):
        # #403: `--arg label` shadowed jq's `label` keyword, so the existence
        # check never parsed and a second claim by the same agent aborted at
        # `gh label create` (the stub's label list already carries agent:fable
        # and `label create` exits 1). A green claim proves the existence
        # check ran and skipped creation.
        result = self.run_claim(self.issue(["task:ready"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claimed #42 in draft PR", result.stdout)

    def test_anothers_label_is_still_a_hard_refusal(self):
        result = self.run_claim(self.issue(["agent:sol", "task:ready"]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("already held by agent:sol", result.stderr)

    def test_own_label_with_an_open_claim_pr_is_still_refused_by_the_registry(self):
        pr_list = json.dumps(
            [
                {
                    "number": 88,
                    "title": "self-filed follow-up (#42)",
                    "body": "Closes #42",
                    "headRefName": "agent/fable-issue-42",
                    "labels": [{"name": "agent:fable"}],
                    "url": "https://example/pull/88",
                }
            ]
        )
        result = self.run_claim(self.issue(["agent:fable"]), pr_list=pr_list)
        self.assertEqual(result.returncode, 1)
        self.assertIn("an open PR already holds it", result.stderr)

    def test_an_unqueued_issue_with_no_labels_at_all_is_claimable(self):
        # #366's second data set: refusing an unlabelled issue forced an agent
        # to self-label to get past the check — the same manual bypass through
        # a different door. Absence of curation is not "not ready".
        result = self.run_claim(self.issue([]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claiming unqueued #42", result.stdout)
        self.assertIn("claimed #42 in draft PR", result.stdout)

    def test_an_explicit_non_ready_task_state_still_refuses_by_name(self):
        result = self.run_claim(self.issue(["task:active"]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("task state is task:active", result.stderr)

    def test_task_blocked_still_refuses(self):
        result = self.run_claim(self.issue(["task:blocked"]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("task:blocked", result.stderr)

    def test_claim_body_records_the_reachable_channel_defaulting_to_board(self):
        # #360: the roster records a channel, never a session name; board is
        # the only channel guaranteed to span every lineage and machine.
        result = self.run_claim(self.issue(["task:ready"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = (self.bin / "captured-pr-body").read_text(encoding="utf-8")
        self.assertIn("**Reachable:** board", body)
        self.assertIn("**From:** fable", body)
        self.assertIn("Closes #42", body)

    def test_claim_reachable_override_is_recorded_verbatim(self):
        env = self.git_env()
        env.update(
            {
                "CLAIM_AGENT": "fable",
                "CLAIM_TEST_ISSUE_JSON": json.dumps(self.issue(["task:ready"])),
                "CLAIM_TEST_PR_LIST": "[]",
                "CLAIM_TEST_REMOVE_READY_EXIT": "0",
                "CLAIM_TEST_BODY_CAPTURE": bash_path(self.bin / "captured-pr-body"),
                "CLAIM_REACHABLE": "session R2 Fable",
            }
        )
        result = run_with_bash_path(
            ["bash", bash_path(SCRIPT), "42"],
            stub_directory=self.bin, env=env, cwd=self.clone,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = (self.bin / "captured-pr-body").read_text(encoding="utf-8")
        self.assertIn("**Reachable:** session R2 Fable", body)

    def test_failed_task_ready_removal_cannot_abort_a_finished_claim(self):
        result = self.run_claim(self.issue(["agent:fable"]), remove_ready_exit="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claimed #42 in draft PR", result.stdout)


class OrientReachabilityFilterTest(unittest.TestCase):
    """#360: the shipped reachability filter against fixtures — channel from
    the claim body, board-assumed when absent, agent-labelled lanes only."""

    def extract_filter(self) -> str:
        text = ORIENT.read_text(encoding="utf-8")
        anchor = text.index("reachable: ")
        start = text.rindex("| jq -r '", 0, anchor) + len("| jq -r '")
        end = text.index("'", start)
        return text[start:end]

    def run_filter(self, prs):
        return subprocess.run(
            ["jq", "-r", self.extract_filter()],
            input=json.dumps(prs), text=True, encoding="utf-8", capture_output=True, check=True,
        ).stdout

    def test_channel_read_from_the_claim_body_with_last_seen(self):
        out = self.run_filter(
            [
                {"number": 373, "labels": [{"name": "agent:fable"}, {"name": "task:active"}],
                 "body": "**From:** fable\n\n**Reachable:** session R2 Fable\n\nCloses #360",
                 "updatedAt": "2026-08-27T06:30:00Z"},
                {"number": 374, "labels": [{"name": "agent:sol"}],
                 "body": "review lane, no roster line", "updatedAt": "2026-08-27T06:00:00Z"},
                {"number": 375, "labels": [{"name": "task:review"}],
                 "body": "no agent label", "updatedAt": "x"},
            ]
        )
        self.assertIn("#373 [agent:fable] reachable: session R2 Fable · last seen 2026-08-27T06:30:00Z", out)
        self.assertIn("#374 [agent:sol] reachable: board (assumed — no roster line)", out)
        self.assertNotIn("#375", out)

    def test_appended_reachable_line_overrides_the_first_one(self):
        # #390: `capture(...)` returns only the first match, so an agent that
        # moves between sessions and appends `**Reachable:** session new`
        # rather than rewriting the body silently keeps the stale channel.
        # The shipped filter must take the LAST **Reachable:** line, because
        # that is how a person reading the thread top-to-bottom would update
        # their belief about where the owner is reachable.
        out = self.run_filter(
            [
                {"number": 374, "labels": [{"name": "agent:fable"}],
                 "body": "**From:** fable\n\n**Reachable:** session old\n\nsome text\n\n**Reachable:** session new",
                 "updatedAt": "2026-08-27T06:35:00Z"},
            ]
        )
        self.assertIn("#374 [agent:fable] reachable: session new", out)
        self.assertNotIn("session old", out)

    def test_last_reachable_wins_even_when_first_is_more_recent_in_text_order(self):
        # Same shape as above with three updates, to prove LAST, not
        # LAST-OF-TWO, and not "the unique line after a fence".
        out = self.run_filter(
            [
                {"number": 374, "labels": [{"name": "agent:fable"}],
                 "body": "**Reachable:** session one\n\n**Reachable:** session two\n\n**Reachable:** session three",
                 "updatedAt": "2026-08-27T06:35:00Z"},
            ]
        )
        self.assertIn("#374 [agent:fable] reachable: session three", out)
        self.assertNotIn("session one", out)
        self.assertNotIn("session two", out)

    def test_no_reachable_line_still_falls_back_to_board_assumed(self):
        # Regression guard: when the body has no `**Reachable:**` marker at
        # all, the LAST-wins change must not break the existing fallback.
        out = self.run_filter(
            [
                {"number": 374, "labels": [{"name": "agent:fable"}],
                 "body": "**From:** fable\n\nno roster line, just prose",
                 "updatedAt": "2026-08-27T06:35:00Z"},
            ]
        )
        self.assertIn("#374 [agent:fable] reachable: board (assumed — no roster line)", out)


class OrientHoldHolderFilterTest(unittest.TestCase):
    """#373's P1: the unreachable agent in the incident was the HOLD owner,
    whom no label records — the review stream does. The shipped filter that
    extracts agents whose latest verdict is changes-required, against a
    fixture shaped like #356 (sol blocking, opus-5 accepting)."""

    def extract_filter(self) -> str:
        text = ORIENT.read_text(encoding="utf-8")
        anchor = text.index('select(.verdict == "changes required")')
        start = text.rindex("| jq -r '", 0, anchor) + len("| jq -r '")
        end = text.index("'", start)
        return text[start:end]

    def run_filter(self, reviews):
        return subprocess.run(
            ["jq", "-r", self.extract_filter()],
            input=json.dumps(reviews), text=True, encoding="utf-8", capture_output=True, check=True,
        ).stdout

    def test_latest_changes_required_agents_extracted_like_356(self):
        reviews = [
            {"body": "**From:** sol\n\n**Verdict:** changes required\n\nfinding",
             "submitted_at": "2026-08-27T05:00:00Z"},
            {"body": "**From:** opus-5\n\n**Verdict:** accept\n\nfine",
             "submitted_at": "2026-08-27T05:01:00Z"},
            {"body": "no header drive-by", "submitted_at": "2026-08-27T05:02:00Z"},
        ]
        out = self.run_filter(reviews)
        self.assertIn("sol", out)
        self.assertNotIn("opus-5", out)

    def test_a_holder_whose_later_verdict_accepts_is_no_longer_listed(self):
        reviews = [
            {"body": "**From:** sol\n\n**Verdict:** changes required\n\nround one",
             "submitted_at": "2026-08-27T05:00:00Z"},
            {"body": "**From:** sol\n\n**Verdict:** accept\n\nfixed",
             "submitted_at": "2026-08-27T06:00:00Z"},
        ]
        out = self.run_filter(reviews)
        self.assertNotIn("sol", out.strip().split("\n"))


class OrientNamedHoldLabelFilterTest(unittest.TestCase):
    """#385: since a review hold now names its holder directly
    (hold:review:<agent>), orient.sh reads that label instead of
    reconstructing ownership from the review stream — no heuristic, no
    review-history fetch, just the label the same way gate.sh reads it."""

    def extract_filter(self) -> str:
        text = ORIENT.read_text(encoding="utf-8")
        anchor = text.index('ltrimstr("hold:review:")')
        start = text.rindex("jq -r '", 0, anchor) + len("jq -r '")
        end = text.index("'", start)
        return text[start:end]

    def run_filter(self, prs):
        return subprocess.run(
            ["jq", "-r", self.extract_filter()],
            input=json.dumps(prs), text=True, encoding="utf-8", capture_output=True, check=True,
        ).stdout

    def test_named_holder_extracted_directly_from_the_label(self):
        prs = [
            {"number": 411, "labels": [{"name": "hold:review:luna"}, {"name": "agent:fable"}]},
        ]
        out = self.run_filter(prs)
        self.assertIn("411\tluna", out)

    def test_pr_with_no_named_hold_label_produces_nothing(self):
        prs = [
            {"number": 415, "labels": [{"name": "agent:fable"}, {"name": "task:review"}]},
        ]
        out = self.run_filter(prs)
        self.assertEqual(out.strip(), "")

    def test_two_named_holders_on_one_pr_each_get_their_own_row(self):
        prs = [
            {"number": 411, "labels": [{"name": "hold:review:sol"}, {"name": "hold:review:luna"}]},
        ]
        out = self.run_filter(prs)
        self.assertIn("411\tsol", out)
        self.assertIn("411\tluna", out)

    def test_a_bare_legacy_hold_review_label_is_not_matched_here(self):
        # The unattributed pre-#385 label has no agent suffix to extract —
        # that PR falls through to the separate legacy-fallback block, not
        # this one, so this filter must produce nothing for it.
        prs = [
            {"number": 420, "labels": [{"name": "hold:review"}]},
        ]
        out = self.run_filter(prs)
        self.assertEqual(out.strip(), "")


class OrientWindowBoundaryTest(unittest.TestCase):
    """#373 round two: the claim-to-PR window check must not substring-match
    issue numbers — #36 vanished whenever any PR body mentioned #365. The
    shipped grep pattern, extracted and exercised."""

    def extract_pattern(self) -> str:
        text = ORIENT.read_text(encoding="utf-8")
        anchor = text.index('grep -qE "#${inum}')
        start = text.index('"#', anchor) + 1
        end = text.index('"', start)
        return text[start:end]

    def matches(self, inum: str, body: str) -> bool:
        import re as _re
        pattern = self.extract_pattern().replace("${inum}", inum).replace("\\$", "$")
        return _re.search(pattern, body) is not None

    def test_prefix_numbers_do_not_swallow_the_issue(self):
        self.assertFalse(self.matches("36", "refs #3650 and nothing else"))
        self.assertFalse(self.matches("365", "see #3650"))

    def test_real_references_still_match(self):
        self.assertTrue(self.matches("365", "Closes #365"))
        self.assertTrue(self.matches("365", "see #365, then merge"))
        self.assertTrue(self.matches("36", "ends with #36"))


class OrientUnqueuedFilterTest(unittest.TestCase):
    """#366's discovery half: issues with no task:* and no agent:* label were
    invisible to orientation for two missions. The shipped filter, against
    fixtures."""

    def extract_filter(self) -> str:
        text = ORIENT.read_text(encoding="utf-8")
        anchor = text.index("unqueued — no task label")
        start = text.rindex("--jq '", 0, anchor) + len("--jq '")
        end = text.index("'", start)
        return text[start:end]

    def run_filter(self, issues):
        return subprocess.run(
            ["jq", "-r", self.extract_filter()],
            input=json.dumps(issues),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        ).stdout

    def test_surfaces_only_truly_unqueued_issues(self):
        out = self.run_filter(
            [
                {"number": 360, "title": "roster gap", "labels": [{"name": "bug"}]},
                {"number": 359, "title": "gate", "labels": [{"name": "task:done"}]},
                {"number": 344, "title": "lane", "labels": [{"name": "agent:fable"}]},
                {"number": 1, "title": "halt", "labels": [{"name": "ops:halt"}]},
            ]
        )
        self.assertIn("#360 (unqueued — no task label)", out)
        self.assertNotIn("#359", out)
        self.assertNotIn("#344", out)
        self.assertNotIn("#1 ", out)


if __name__ == "__main__":
    unittest.main()
