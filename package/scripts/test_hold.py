import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path

SCRIPT = Path(__file__).with_name("hold.sh")


class HoldTestCase(unittest.TestCase):
    """Shared gh-stub harness for hold.sh — no test_ methods of its own.

    The stub tracks a simulated PR label set (HOLD_TEST_PR_LABELS) so
    `pr view --json labels --jq ...` reflects prior `pr edit
    --add-label`/`--remove-label` calls within the same test, the way real
    `gh` would. HOLD_TEST_REMOVAL_ACTUALLY_FAILS simulates `--remove-label`
    exiting 0 without the label actually leaving the PR (#420 review P1);
    HOLD_TEST_COMMENT_FAILS simulates a transient `pr comment` failure
    (#420 review P2).
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.bin = base / "bin"
        self.bin.mkdir()
        self.gh_log = base / "gh.log"
        self.existing_labels = base / "existing-labels.json"
        self.existing_labels.write_text("[]", encoding="utf-8")
        self.pr_labels = base / "pr-labels.json"
        self.pr_labels.write_text("[]", encoding="utf-8")
        self.comment_body = base / "comment-body.txt"
        self.lookup_count = base / "lookup-count.txt"
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                log="$HOLD_TEST_GH_LOG"
                labels_path="$HOLD_TEST_LABELS"
                pr_labels_path="$HOLD_TEST_PR_LABELS"
                printf '%s\\n' "$*" >>"$log"
                case "$1 $2" in
                  "repo view")
                    printf 'example/canonical\\n'
                    ;;
                  "pr view")
                    json_fields=""
                    jq_prog=""
                    prev=""
                    for arg in "$@"; do
                      if [ "$prev" = "--json" ]; then json_fields="$arg"; fi
                      if [ "$prev" = "--jq" ]; then jq_prog="$arg"; fi
                      prev="$arg"
                    done
                    if [ "$json_fields" = "labels" ]; then
                      count_path="$HOLD_TEST_LOOKUP_COUNT"
                      n=$(( $(cat "$count_path" 2>/dev/null || echo 0) + 1 ))
                      printf '%s' "$n" >"$count_path"
                      if [ "$n" = "${HOLD_TEST_LOOKUP_FAIL_ON_CALL:-0}" ]; then
                        echo "simulated transient lookup failure" >&2
                        exit 1
                      fi
                      pr_obj=$(jq -n --slurpfile names "$pr_labels_path" \\
                        '{labels: ($names[0] | map({name: .}))}')
                      if [ -n "$jq_prog" ]; then
                        printf '%s' "$pr_obj" | jq -r "$jq_prog"
                      else
                        printf '%s\\n' "$pr_obj"
                      fi
                    else
                      jq -n --arg state "${HOLD_TEST_STATE:-OPEN}" '{number: 7, state: $state}'
                    fi
                    ;;
                  "label list")
                    cat "$labels_path"
                    ;;
                  "label create")
                    name="$3"
                    jq --arg n "$name" '. + [{"name": $n}]' "$labels_path" >"$labels_path.tmp"
                    mv "$labels_path.tmp" "$labels_path"
                    ;;
                  "pr edit")
                    prev=""
                    for arg in "$@"; do
                      if [ "$prev" = "--add-label" ]; then
                        jq --arg n "$arg" '(. + [$n]) | unique' "$pr_labels_path" >"$pr_labels_path.tmp"
                        mv "$pr_labels_path.tmp" "$pr_labels_path"
                      fi
                      if [ "$prev" = "--remove-label" ] && [ -z "${HOLD_TEST_REMOVAL_ACTUALLY_FAILS:-}" ]; then
                        jq --arg n "$arg" 'map(select(. != $n))' "$pr_labels_path" >"$pr_labels_path.tmp"
                        mv "$pr_labels_path.tmp" "$pr_labels_path"
                      fi
                      prev="$arg"
                    done
                    ;;
                  "pr comment")
                    if [ -n "${HOLD_TEST_COMMENT_FAILS:-}" ]; then
                      echo "simulated transient comment failure" >&2
                      exit 1
                    fi
                    prev=""
                    for arg in "$@"; do
                      if [ "$prev" = "--body-file" ]; then
                        cp "$arg" "$HOLD_TEST_COMMENT"
                      fi
                      prev="$arg"
                    done
                    ;;
                  *)
                    echo "unexpected gh: $*" >&2
                    exit 1
                    ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.cwd = base

    def tearDown(self):
        self.dir.cleanup()

    def seed_pr_labels(self, labels: list[str]) -> None:
        self.pr_labels.write_text(json.dumps(labels), encoding="utf-8")

    def pr_labels_now(self) -> list[str]:
        return json.loads(self.pr_labels.read_text(encoding="utf-8"))

    def run_hold(
        self,
        *args: str,
        agent: str = "sol",
        state: str = "OPEN",
        removal_actually_fails: bool = False,
        comment_fails: bool = False,
        lookup_fails_on_call: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOLD_TEST_GH_LOG"] = bash_path(self.gh_log)
        env["HOLD_TEST_LABELS"] = bash_path(self.existing_labels)
        env["HOLD_TEST_PR_LABELS"] = bash_path(self.pr_labels)
        env["HOLD_TEST_COMMENT"] = bash_path(self.comment_body)
        env["HOLD_TEST_LOOKUP_COUNT"] = bash_path(self.lookup_count)
        env["HOLD_TEST_STATE"] = state
        env["CLAIM_AGENT"] = agent
        if removal_actually_fails:
            env["HOLD_TEST_REMOVAL_ACTUALLY_FAILS"] = "1"
        elif "HOLD_TEST_REMOVAL_ACTUALLY_FAILS" in env:
            del env["HOLD_TEST_REMOVAL_ACTUALLY_FAILS"]
        if comment_fails:
            env["HOLD_TEST_COMMENT_FAILS"] = "1"
        elif "HOLD_TEST_COMMENT_FAILS" in env:
            del env["HOLD_TEST_COMMENT_FAILS"]
        if lookup_fails_on_call is not None:
            env["HOLD_TEST_LOOKUP_FAIL_ON_CALL"] = str(lookup_fails_on_call)
        elif "HOLD_TEST_LOOKUP_FAIL_ON_CALL" in env:
            del env["HOLD_TEST_LOOKUP_FAIL_ON_CALL"]
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return run_with_bash_path(
            ["bash", str(SCRIPT), *args],
            stub_directory=self.bin,
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
        )


class HoldReviewTest(HoldTestCase):
    """Hermetic regressions for hold.sh: named per-holder review holds (#385)."""

    def test_add_creates_the_named_label_and_applies_it(self):
        result = self.run_hold("review", "add", "7", agent="sol")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("set hold:review:sol on PR #7", result.stdout)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("label create hold:review:sol", log)
        self.assertIn("pr edit 7 --repo example/canonical --add-label hold:review:sol", log)
        self.assertIn("hold:review:sol", self.pr_labels_now())

    def test_add_does_not_recreate_an_existing_label(self):
        self.existing_labels.write_text('[{"name": "hold:review:sol"}]', encoding="utf-8")
        result = self.run_hold("review", "add", "7", agent="sol")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("label create", log)

    def test_clear_removes_only_the_callers_own_named_label(self):
        self.seed_pr_labels(["hold:review:sol"])
        result = self.run_hold("review", "clear", "7", agent="sol")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cleared hold:review:sol on PR #7", result.stdout)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("pr edit 7 --repo example/canonical --remove-label hold:review:sol", log)
        self.assertNotIn("hold:review:luna", log)
        self.assertNotIn("hold:review:sol", self.pr_labels_now())

    def test_different_agents_touch_different_labels(self):
        sol_result = self.run_hold("review", "add", "7", agent="sol")
        luna_result = self.run_hold("review", "add", "7", agent="luna")
        self.assertEqual(sol_result.returncode, 0, sol_result.stdout + sol_result.stderr)
        self.assertEqual(luna_result.returncode, 0, luna_result.stdout + luna_result.stderr)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("add-label hold:review:sol", log)
        self.assertIn("add-label hold:review:luna", log)

    def test_refuses_a_closed_pr(self):
        result = self.run_hold("review", "add", "7", agent="sol", state="MERGED")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("state is MERGED", result.stderr)

    def test_refuses_an_invalid_agent_id(self):
        result = self.run_hold("review", "add", "7", agent="Not Valid!")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("lower-case stable agent id", result.stderr)

    def test_refuses_unknown_action(self):
        result = self.run_hold("review", "delete", "7", agent="sol")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("usage:", result.stderr)

    def test_refuses_non_review_kind(self):
        result = self.run_hold("author", "add", "7", agent="sol")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("usage:", result.stderr)

    def test_clear_of_an_already_absent_label_still_reports_success(self):
        # Plain self-clear is idempotent: the label is already gone, and
        # verification correctly finds it absent either way.
        result = self.run_hold("review", "clear", "7", agent="sol")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cleared hold:review:sol on PR #7", result.stdout)


class HoldStewardTransferTest(HoldTestCase):
    """The steward path: clearing a *different* agent's hold, never silently."""

    def test_steward_clear_requires_an_explicit_discharge_classification(self):
        self.seed_pr_labels(["hold:review:luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--reason", "pushed fix at abc1234",
            agent="opus-5",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--discharge verifiable", result.stderr)
        self.assertIn("hold:review:luna", self.pr_labels_now())

    def test_judgment_finding_is_refused_without_comment_or_label_mutation(self):
        self.seed_pr_labels(["hold:review:luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--discharge", "judgment",
            "--reason", "the steward considers the trade acceptable",
            agent="opus-5",
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("judgment finding", result.stderr)
        self.assertFalse(self.gh_log.exists())
        self.assertIn("hold:review:luna", self.pr_labels_now())

    def test_steward_clear_refuses_an_unknown_discharge_classification(self):
        self.seed_pr_labels(["hold:review:luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--discharge", "subjective",
            "--reason", "the steward agrees",
            agent="opus-5",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("must be verifiable or judgment", result.stderr)
        self.assertFalse(self.gh_log.exists())
        self.assertIn("hold:review:luna", self.pr_labels_now())

    def test_discharge_classification_without_steward_is_refused(self):
        self.seed_pr_labels(["hold:review:opus-5"])
        result = self.run_hold(
            "review", "clear", "7",
            "--discharge", "verifiable", "--reason", "head contains main",
            agent="opus-5",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--steward, --discharge verifiable, and --reason", result.stderr)
        self.assertIn("hold:review:opus-5", self.pr_labels_now())

    def test_verifiable_clear_records_its_classification(self):
        self.seed_pr_labels(["hold:review:luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--discharge", "verifiable",
            "--reason", "head abc1234 contains current main",
            agent="opus-5",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        comment = self.comment_body.read_text(encoding="utf-8")
        self.assertIn("Discharge classification: verifiable", comment)
        self.assertNotIn("hold:review:luna", self.pr_labels_now())

    def test_steward_clear_removes_the_named_holders_label_and_records_evidence(self):
        self.seed_pr_labels(["hold:review:luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--discharge", "verifiable",
            "--reason", "pushed fix at abc1234, luna's finding no longer applies",
            agent="opus-5",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("steward-cleared hold:review:luna", result.stdout)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("pr edit 7 --repo example/canonical --remove-label hold:review:luna", log)
        self.assertIn("pr comment", log)
        comment = self.comment_body.read_text(encoding="utf-8")
        self.assertIn("**From:** opus-5", comment)
        self.assertIn("hold:review:luna", comment)
        self.assertIn("luna", comment)
        self.assertIn("Discharge classification: verifiable", comment)
        self.assertIn("pushed fix at abc1234", comment)
        self.assertNotIn("hold:review:luna", self.pr_labels_now())

    def test_steward_clear_never_touches_a_different_holders_label(self):
        self.seed_pr_labels(["hold:review:luna", "hold:review:sol"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--discharge", "verifiable", "--reason", "discharged",
            agent="opus-5",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("hold:review:opus-5", log)
        self.assertNotIn("remove-label hold:review:sol", log)
        self.assertIn("hold:review:sol", self.pr_labels_now())

    def test_steward_flag_requires_a_reason(self):
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--discharge", "verifiable",
            agent="opus-5",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--steward, --discharge verifiable, and --reason must all be given", result.stderr)

    def test_reason_alone_without_steward_is_refused(self):
        result = self.run_hold("review", "clear", "7", "--reason", "discharged", agent="opus-5")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--steward, --discharge verifiable, and --reason must all be given", result.stderr)

    def test_steward_cannot_target_their_own_id(self):
        result = self.run_hold(
            "review", "clear", "7", "--steward", "opus-5", "--discharge", "verifiable",
            "--reason", "discharged", agent="opus-5",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("your own id", result.stderr)

    def test_steward_flags_are_refused_on_add(self):
        result = self.run_hold(
            "review", "add", "7", "--steward", "luna", "--discharge", "verifiable",
            "--reason", "discharged", agent="opus-5",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("only apply to clear", result.stderr)

    def test_plain_clear_by_the_steward_only_clears_their_own_label(self):
        # No --steward given: opus-5 clearing without it clears opus-5's own
        # label, never luna's, even though luna is mentioned nowhere here —
        # this is the regression for "the tool defaults to self, not to
        # whichever hold happens to be open."
        self.seed_pr_labels(["hold:review:opus-5", "hold:review:luna"])
        result = self.run_hold("review", "clear", "7", agent="opus-5")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("remove-label hold:review:opus-5", log)
        self.assertNotIn("pr comment", log)
        self.assertIn("hold:review:luna", self.pr_labels_now())


class HoldRemovalVerificationTest(HoldTestCase):
    """#420 review P1/P2: a clear must not report success, or leave durable
    evidence, for a mutation that did not actually happen."""

    def test_a_typo_d_target_that_never_existed_is_refused_before_anything_is_posted(self):
        # The label the typo'd id names ("kiat-lunaa") was never present —
        # the real one ("kiat-luna") is untouched. Refused before the
        # evidence comment is even posted, not silently reported as success.
        self.seed_pr_labels(["hold:review:kiat-luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "kiat-lunaa", "--discharge", "verifiable",
            "--reason", "discharged at aa2b6484",
            agent="opus-5",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not currently set", result.stdout + result.stderr)
        self.assertNotIn("steward-cleared", result.stdout)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("pr comment", log)
        self.assertIn("hold:review:kiat-luna", self.pr_labels_now())

    def test_a_removal_that_silently_does_not_take_is_reported_as_a_failure(self):
        # gh pr edit --remove-label can exit 0 without the label actually
        # leaving the PR (a partial API failure, etc.) even when the label
        # genuinely was present beforehand — the script must not trust the
        # exit code alone.
        self.seed_pr_labels(["hold:review:kiat-luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "kiat-luna", "--discharge", "verifiable",
            "--reason", "discharged at aa2b6484",
            agent="opus-5",
            removal_actually_fails=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still present", result.stdout + result.stderr)
        self.assertNotIn("steward-cleared", result.stdout)

    def test_plain_self_clear_also_fails_loudly_when_the_label_does_not_actually_go(self):
        self.seed_pr_labels(["hold:review:sol"])
        result = self.run_hold(
            "review", "clear", "7", agent="sol", removal_actually_fails=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still present", result.stdout + result.stderr)
        self.assertNotIn("cleared hold:review:sol on PR #7", result.stdout)

    def test_a_failed_evidence_comment_aborts_before_any_label_mutation(self):
        self.seed_pr_labels(["hold:review:luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--discharge", "verifiable", "--reason", "discharged",
            agent="opus-5",
            comment_fails=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hold:review:luna", self.pr_labels_now())
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertNotIn("remove-label", log)

    def test_a_verify_lookup_that_itself_fails_is_never_read_as_gone(self):
        # #420 review, second pass: label_present() used to swallow a gh
        # error into the empty string, and the post-removal check treated
        # anything not literally "true" as gone — an API failure on the
        # verify call therefore read as a successful clear. The pre-check
        # (call #1: label present, comment posted) succeeds; the
        # post-removal check (call #2) is the one that fails here.
        self.seed_pr_labels(["hold:review:luna"])
        result = self.run_hold(
            "review", "clear", "7",
            "--steward", "luna", "--discharge", "verifiable", "--reason", "discharged",
            agent="opus-5",
            lookup_fails_on_call=2,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not confirm", result.stdout + result.stderr)
        self.assertNotIn("steward-cleared", result.stdout)

    def test_a_verify_lookup_failure_on_plain_self_clear_is_also_not_reported_as_success(self):
        self.seed_pr_labels(["hold:review:sol"])
        result = self.run_hold(
            "review", "clear", "7", agent="sol", lookup_fails_on_call=1,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not confirm", result.stdout + result.stderr)
        self.assertNotIn("cleared hold:review:sol on PR #7", result.stdout)


class HoldLegacyClearTest(HoldTestCase):
    """#420 review P3: hold.sh must be able to clear the unattributed
    bare `hold:review` label gate.sh still blocks on during migration."""

    def test_legacy_clear_removes_the_bare_label_with_a_recorded_reason(self):
        self.seed_pr_labels(["hold:review"])
        result = self.run_hold(
            "review", "clear", "7", "--legacy", "--reason", "superseded by #418", agent="fable",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cleared legacy hold:review", result.stdout)
        log = self.gh_log.read_text(encoding="utf-8")
        self.assertIn("pr edit 7 --repo example/canonical --remove-label hold:review", log)
        comment = self.comment_body.read_text(encoding="utf-8")
        self.assertIn("hold:review", comment)
        self.assertIn("superseded by #418", comment)
        self.assertNotIn("hold:review", self.pr_labels_now())

    def test_legacy_requires_a_reason(self):
        result = self.run_hold("review", "clear", "7", "--legacy", agent="fable")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--legacy requires --reason", result.stderr)

    def test_legacy_and_steward_are_mutually_exclusive(self):
        result = self.run_hold(
            "review", "clear", "7", "--legacy", "--steward", "luna", "--reason", "x", agent="fable",
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("mutually exclusive", result.stderr)

    def test_legacy_never_touches_a_named_holders_label(self):
        self.seed_pr_labels(["hold:review", "hold:review:sol"])
        result = self.run_hold(
            "review", "clear", "7", "--legacy", "--reason", "discharged", agent="fable",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hold:review:sol", self.pr_labels_now())


if __name__ == "__main__":
    unittest.main()
