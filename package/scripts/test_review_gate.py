import json
import os
import shutil
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

    def run_standalone_gate(self, *, fixture=False, repository="example/canonical"):
        """Run a copy that has neither `_default_branch.sh` nor a checkout."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            standalone = root / "review_gate.sh"
            shutil.copyfile(SCRIPT, standalone)
            standalone.chmod(0o755)

            fixture_file = root / "fixture.json"
            fixture_file.write_text(
                json.dumps(
                    {
                        "reviewed": SHA,
                        "labels": ["agent:author"],
                        "reviews": [self.review()],
                    }
                ),
                encoding="utf-8",
            )

            gh = root / "gh"
            gh.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
case "${{1:-}} ${{2:-}}" in
  "pr view")
    [[ "$*" == *"--repo example/canonical"* ]] || exit 81
    printf '%s\n' '{{"headRefOid":"{SHA}","labels":[{{"name":"agent:author"}}]}}'
    ;;
  api\\ *)
    [[ "$*" == *"repos/example/canonical/pulls/7/reviews"* ]] || exit 82
    printf '%s\n' '[{{"id":1,"state":"COMMENTED","body":"**From:** reviewer\\n\\n**Verdict:** accept","commit_id":"{SHA}","submitted_at":"2026-01-01T00:00:00Z"}}]'
    ;;
  *) exit 83 ;;
esac
""",
                encoding="utf-8",
                newline="\n",
            )
            gh.chmod(0o755)

            git = root / "git"
            git.write_text(
                "#!/usr/bin/env bash\necho 'standalone gate touched git/origin' >&2\nexit 84\n",
                encoding="utf-8",
                newline="\n",
            )
            git.chmod(0o755)

            env = os.environ.copy()
            env.pop("AI_TEAM_TEST_ORIGIN_REPO", None)
            env.pop("REVIEW_GATE_INPUT", None)
            if fixture:
                env["REVIEW_GATE_INPUT"] = str(fixture_file)
                # Fixture evaluation must not validate or consult the live
                # repository override either.
                env["REVIEW_GATE_REPOSITORY"] = "not/a/valid/repository"
                arguments = []
            else:
                if repository is None:
                    env.pop("REVIEW_GATE_REPOSITORY", None)
                else:
                    env["REVIEW_GATE_REPOSITORY"] = repository
                arguments = ["7", SHA]

            return run_with_bash_path(
                ["bash", bash_path(standalone), *arguments],
                stub_directory=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def run_live_gate_with_argv_guard(
        self,
        review_body_size=50_000,
        jq_arg_limit=4_096,
        *,
        malformed_reviews=False,
        interrupt_on_review_parse=False,
        repository_override=None,
    ):
        """Exercise the live GitHub path while a jq shim rejects large argv.

        Windows rejects a large review payload before jq starts. The shim gives
        Linux the same bounded-argument contract, so this regression fails on
        the old `--argjson reviews "$reviews"` implementation everywhere.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temp_files = root / "tmp"
            temp_files.mkdir()

            gh = root / "gh"
            gh.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}} ${{2:-}}" = "pr view" ]; then
  [[ "$*" == *"--repo example/canonical"* ]] || exit 85
  printf '%s\n' '{{"headRefOid":"{SHA}","labels":[{{"name":"agent:author"}}]}}'
elif [ "${{1:-}}" = "api" ]; then
  [[ "$*" == *"repos/example/canonical/pulls/7/reviews"* ]] || exit 86
  if [ "${{MALFORMED_REVIEWS:-0}}" = 1 ]; then
    printf '{{'
    exit 0
  fi
  padding=$(printf '%*s' "${{REVIEW_BODY_SIZE:?}}" '')
  padding=${{padding// /x}}
  printf '%s\n' '[{{"id":1,"state":"COMMENTED","body":"**From:** reviewer\\n\\n**Verdict:** accept\\n'"$padding"'","commit_id":"{SHA}","submitted_at":"2026-01-01T00:00:00Z"}}]'
else
  printf 'unexpected gh command: %s\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
                newline="\n",
            )
            gh.chmod(0o755)

            jq = root / "jq"
            jq.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
  if (( ${#arg} > JQ_ARG_LIMIT )); then
    printf 'jq argument exceeded test limit: %s > %s\n' "${#arg}" "$JQ_ARG_LIMIT" >&2
    exit 91
  fi
done
if [ "${PAUSE_ON_SLURP:-0}" = 1 ] && [ "${1:-}" = -s ]; then
  : > "$SIGNAL_MARKER"
  sleep 2
fi
exec "$REAL_JQ" "$@"
""",
                encoding="utf-8",
                newline="\n",
            )
            jq.chmod(0o755)

            env = os.environ.copy()
            env.pop("REVIEW_GATE_INPUT", None)
            env.pop("REVIEW_GATE_REPOSITORY", None)
            env["AI_TEAM_TEST_ORIGIN_REPO"] = "example/canonical"
            if repository_override is not None:
                env["REVIEW_GATE_REPOSITORY"] = repository_override
            env["JQ_ARG_LIMIT"] = str(jq_arg_limit)
            real_jq = shutil.which("jq")
            if real_jq is None:
                self.fail("jq is required to exercise the review gate")
            env["REAL_JQ"] = bash_path(Path(real_jq))
            env["REVIEW_BODY_SIZE"] = str(review_body_size)
            env["MALFORMED_REVIEWS"] = "1" if malformed_reviews else "0"
            env["TMPDIR"] = str(temp_files)
            signal_marker = root / "review-parse-started"
            env["PAUSE_ON_SLURP"] = "1" if interrupt_on_review_parse else "0"
            env["SIGNAL_MARKER"] = bash_path(signal_marker)

            command = ["bash", bash_path(SCRIPT), "7", SHA]
            if interrupt_on_review_parse:
                signal_driver = """set -euo pipefail
target=$1
shift
bash "$target" "$@" &
pid=$!
ready=false
for _ in $(seq 1 100); do
  if [ -f "$SIGNAL_MARKER" ]; then
    ready=true
    break
  fi
  sleep 0.05
done
if [ "$ready" != true ]; then
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  echo "review parser did not reach the signal barrier" >&2
  exit 98
fi
kill -TERM "$pid"
set +e
wait "$pid"
rc=$?
set -e
printf 'signal-exit=%s\n' "$rc"
[ "$rc" -eq 143 ]
"""
                command = [
                    "bash",
                    "-c",
                    signal_driver,
                    "review-gate-signal-test",
                    bash_path(SCRIPT),
                    "7",
                    SHA,
                ]

            result = run_with_bash_path(
                command,
                stub_directory=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            leftovers = list(temp_files.iterdir())

        return result, leftovers

    def test_commented_exact_head_acceptance_passes(self):
        result = self.run_gate([self.review()])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)

    def test_standalone_fixture_mode_never_sources_the_origin_helper(self):
        result = self.run_standalone_gate(fixture=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)

    def test_standalone_live_mode_uses_the_explicit_repository(self):
        result = self.run_standalone_gate()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)

    def test_standalone_live_mode_requires_a_valid_repository(self):
        missing = self.run_standalone_gate(repository=None)
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        self.assertIn("REVIEW_GATE_REPOSITORY is required", missing.stderr)

        invalid = self.run_standalone_gate(repository="example/canonical/extra")
        self.assertEqual(invalid.returncode, 2, invalid.stdout + invalid.stderr)
        self.assertIn("must be an owner/repository name", invalid.stderr)

    def test_local_live_mode_still_falls_back_to_the_origin_helper(self):
        result, leftovers = self.run_live_gate_with_argv_guard(review_body_size=1)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)
        self.assertEqual(leftovers, [])

    def test_local_origin_ignores_an_inherited_repository_override(self):
        result, leftovers = self.run_live_gate_with_argv_guard(
            review_body_size=1,
            repository_override="attacker/repository",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)
        self.assertEqual(leftovers, [])

    def test_large_live_review_history_never_enters_jq_argv(self):
        result, leftovers = self.run_live_gate_with_argv_guard()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)
        self.assertEqual(leftovers, [], f"temporary review files leaked: {leftovers}")

    def test_live_review_parse_failure_cleans_temporary_files(self):
        result, leftovers = self.run_live_gate_with_argv_guard(malformed_reviews=True)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot read reviews", result.stderr)
        self.assertEqual(leftovers, [], f"temporary review files leaked after failure: {leftovers}")

    def test_signal_during_live_review_parse_cleans_temporary_files(self):
        result, leftovers = self.run_live_gate_with_argv_guard(interrupt_on_review_parse=True)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("signal-exit=143", result.stdout)
        self.assertEqual(leftovers, [], f"temporary review files leaked after TERM: {leftovers}")

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
