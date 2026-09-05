import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


SCRIPT = Path(__file__).with_name("review_gate.sh")
SHA = "a" * 40
STALE_SHA = "b" * 40


class GateHarness(unittest.TestCase):
    """Fixture-mode helpers shared by every gate test class."""

    def run_gate(
        self,
        reviews,
        labels=("agent:author",),
        reviewed=SHA,
        head_sha=SHA,
        identity=None,
        comments=(),
        cwd=None,
    ):
        if identity is None:
            identity = {
                "user": {"id": 1, "login": "human-author", "type": "User"},
                "head": {"repo": {"id": 100}},
                "base": {"repo": {"id": 100}},
            }
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "review.json"
            fixture.write_text(
                json.dumps({
                    "reviewed": reviewed,
                    "head_sha": head_sha,
                    "labels": list(labels),
                    "identity": identity,
                    "reviews": reviews,
                    "comments": list(comments),
                }),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["REVIEW_GATE_INPUT"] = str(fixture)
            return run_with_bash_path(
                ["bash", bash_path(SCRIPT)],
                stub_directory=Path(directory),
                env=env,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )

    def git(self, repository, *arguments, check=True, input=None):
        return subprocess.run(
            [
                "git",
                "-c", "user.name=Review Gate Test",
                "-c", "user.email=review-gate@example.invalid",
                *arguments,
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            check=check,
            input=input,
        )

    def commit_file(self, repository, path, content, message):
        file = repository / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        self.git(repository, "add", path)
        self.git(repository, "commit", "-qm", message)
        return self.git(repository, "rev-parse", "HEAD").stdout.strip()

    def clean_merge_history(self):
        directory = tempfile.TemporaryDirectory()
        repository = Path(directory.name)
        self.git(repository, "init", "-q", "-b", "main")
        self.commit_file(repository, "shared.txt", "base\n", "base")
        self.git(repository, "switch", "-qc", "feature")
        accepted = self.commit_file(repository, "feature.txt", "feature\n", "feature")
        self.git(repository, "switch", "-q", "main")
        base = self.commit_file(repository, "main.txt", "main\n", "main")
        self.git(repository, "switch", "-q", "feature")
        self.git(repository, "merge", "--no-ff", "-qm", "merge main", "main")
        head = self.git(repository, "rev-parse", "HEAD").stdout.strip()
        return directory, repository, accepted, base, head

    def run_carry_gate(self, repository, accepted, base, head, reviews=None,
                       base_ref="main"):
        identity = {
            "user": {"id": 1, "login": "human-author", "type": "User"},
            "head": {"repo": {"id": 100}},
            "base": {
                "repo": {"id": 100, "default_branch": "main"},
                "sha": base,
                "ref": base_ref,
            },
        }
        if reviews is None:
            reviews = [self.review(commit_id=accepted, head_marker=accepted)]
        return self.run_gate(
            reviews,
            reviewed=head,
            head_sha=head,
            identity=identity,
            cwd=repository,
        )

    def review(
        self,
        agent="reviewer",
        state="COMMENTED",
        body=None,
        commit_id=SHA,
        at="2026-01-01T00:00:00Z",
        head_marker=None,
        bind_head=True,
        user=None,
    ):
        if body is None:
            body = f"**From:** {agent}\n\n**Verdict:** accept"
        if bind_head and "**HEAD reviewed:**" not in body:
            marker = commit_id if head_marker is None else head_marker
            body = f"{body}\n\n**HEAD reviewed:** `{marker}`"
        return {
            "id": 1,
            "state": state,
            "body": body,
            "commit_id": commit_id,
            "submitted_at": at,
            "user": user
            if user is not None
            else {"id": 900, "login": "agent-account", "type": "User"},
        }


class ReviewGateTest(GateHarness):
    def run_standalone_gate(self, *, fixture=False, repository="example/canonical"):
        """Run a copy that has neither `_default_branch.sh` nor a checkout."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            standalone = root / "review_gate.sh"
            shutil.copyfile(SCRIPT, standalone)
            standalone.chmod(0o755)
            # The trusted workflow fetches the trusted-author helper alongside
            # the grammar from the same pinned commit; mirror that contract.
            helper = root / "_trusted_author.sh"
            shutil.copyfile(SCRIPT.with_name("_trusted_author.sh"), helper)
            helper.chmod(0o755)

            fixture_file = root / "fixture.json"
            fixture_file.write_text(
                json.dumps(
                    {
                        "reviewed": SHA,
                        "head_sha": SHA,
                        "labels": ["agent:author"],
                        "identity": {
                            "user": {"id": 1, "login": "human-author", "type": "User"},
                            "head": {"repo": {"id": 100}},
                            "base": {"repo": {"id": 100}},
                        },
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
  api\\ repos/example/canonical/pulls/7)
    printf '%s\n' '{{"head":{{"sha":"{SHA}","repo":{{"id":100}}}},"base":{{"repo":{{"id":100}}}},"user":{{"id":1,"login":"human-author","type":"User"}},"labels":[{{"name":"agent:author"}}]}}'
    ;;
  api\\ repos/example/canonical/pulls/7/reviews)
    printf '%s\n' '[{{"id":1,"state":"COMMENTED","body":"**From:** reviewer\\n\\n**Verdict:** accept\\n\\n**HEAD reviewed:** `{SHA}`","commit_id":"{SHA}","submitted_at":"2026-01-01T00:00:00Z"}}]'
    ;;
  api\\ repos/example/canonical/issues/7/comments)
    printf '%s\n' '[]'
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
        # Restored to 4096 after #83 moved the filter out of argv. The bound
        # guards unbounded GitHub payloads only; the filter is a fixed reviewed
        # constant loaded via --from-file and no longer shares this ceiling.
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

        The bound is on unbounded GitHub data reaching argv, not on the filter
        text, which is a fixed reviewed constant a few kilobytes long. The
        default review payload here is 50,000 bytes and still trips the shim by
        an order of magnitude.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temp_files = root / "tmp"
            temp_files.mkdir()

            gh = root / "gh"
            gh.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}} ${{2:-}}" = "api repos/example/canonical/pulls/7" ]; then
  printf '%s\n' '{{"head":{{"sha":"{SHA}","repo":{{"id":100}}}},"base":{{"repo":{{"id":100}}}},"user":{{"id":1,"login":"human-author","type":"User"}},"labels":[{{"name":"agent:author"}}]}}'
elif [ "${{1:-}}" = "api" ]; then
  if [[ "$*" == *"repos/example/canonical/issues/7/comments"* ]]; then
    printf '[]\\n'
    exit 0
  fi
  [[ "$*" == *"repos/example/canonical/pulls/7/reviews"* ]] || exit 86
  if [ "${{MALFORMED_REVIEWS:-0}}" = 1 ]; then
    printf '{{'
    exit 0
  fi
  padding=$(printf '%*s' "${{REVIEW_BODY_SIZE:?}}" '')
  padding=${{padding// /x}}
  printf '%s\n' '[{{"id":1,"state":"COMMENTED","body":"**From:** reviewer\\n\\n**Verdict:** accept\\n\\n**HEAD reviewed:** `{SHA}`\\n'"$padding"'","commit_id":"{SHA}","submitted_at":"2026-01-01T00:00:00Z"}}]'
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

    def test_accept_with_follow_up_is_not_a_verdict(self):
        # Owner decision 2026-09-05: two verdicts only. A follow-up is either
        # fixed in this PR or it is changes required; the old form is malformed.
        body = "**From:** reviewer\n\n**Verdict:** accept with follow-up"
        result = self.run_gate([self.review(body=body)])

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("independent exact-head acceptance from reviewer", result.stdout)
        self.assertIn("rejected for format", result.stdout)
        self.assertIn("there is no follow-up verdict", result.stdout)

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

    def test_production_path_streams_large_dependabot_payloads_instead_of_argv(self):
        identity_sentinel = "REVIEW_GATE_IDENTITY_ARGV_SENTINEL"
        review_sentinel = "REVIEW_GATE_REVIEWS_ARGV_SENTINEL"
        identity = self.dependabot_identity()
        identity["head"]["sha"] = SHA
        identity["labels"] = []
        identity["title"] = "Bump laravel/framework"
        identity["body"] = identity_sentinel + ("x" * 60_000)
        identity_json = json.dumps(identity)
        self.assertGreater(len(identity_json.encode("utf-8")), 50 * 1024)

        review = self.review()
        review["body"] = f"{review['body']}\n\n{review_sentinel}"

        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            identity_fixture = directory / "identity.json"
            reviews_fixture = directory / "reviews.json"
            identity_fixture.write_text(identity_json, encoding="utf-8")
            reviews_fixture.write_text(json.dumps([review]), encoding="utf-8")

            real_jq = shutil.which("jq")
            self.assertIsNotNone(real_jq, "jq is required to exercise review_gate.sh")

            env = os.environ.copy()
            env.pop("REVIEW_GATE_INPUT", None)
            env["AI_TEAM_TEST_ORIGIN_REPO"] = "example/canonical"
            env["REVIEW_GATE_TEST_IDENTITY"] = bash_path(identity_fixture)
            env["REVIEW_GATE_TEST_REVIEWS"] = bash_path(reviews_fixture)
            env["REVIEW_GATE_TEST_REAL_JQ"] = bash_path(Path(real_jq))
            production_stubs = textwrap.dedent(
                f"""\
                gh() {{
                  if [ "$1 $2" = "repo view" ]; then
                    printf '%s\\n' 'example/canonical'
                  elif [ "$1 $2" = "api repos/example/canonical/pulls/462" ]; then
                    cat "$REVIEW_GATE_TEST_IDENTITY"
                  elif [ "$1 $2" = "api repos/example/canonical/pulls/462/reviews" ]; then
                    cat "$REVIEW_GATE_TEST_REVIEWS"
                  elif [ "$1 $2" = "api repos/example/canonical/issues/462/comments" ]; then
                    printf '[]\\n'
                  else
                    printf 'unexpected gh call: %s\\n' "$*" >&2
                    return 96
                  fi
                }}
                jq() {{
                  local argument
                  for argument in "$@"; do
                    case "$argument" in
                      *{identity_sentinel}*|*{review_sentinel}*)
                        printf 'GitHub JSON leaked into jq argv\\n' >&2
                        return 97
                        ;;
                    esac
                  done
                  "$REVIEW_GATE_TEST_REAL_JQ" "$@"
                }}
                export -f gh jq
                exec bash "$@"
                """
            )
            result = run_with_bash_path(
                [
                    "bash",
                    "-c",
                    production_stubs,
                    "review-gate-production-test",
                    bash_path(SCRIPT),
                    "462",
                    SHA,
                ],
                stub_directory=directory,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)

    def test_production_path_cleans_first_temp_when_second_allocation_fails(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            first_temp = directory / "first-review-input.json"
            allocation_state = directory / "mktemp-called"
            env = os.environ.copy()
            env.pop("REVIEW_GATE_INPUT", None)
            env["AI_TEAM_TEST_ORIGIN_REPO"] = "example/canonical"
            env["REVIEW_GATE_TEST_FIRST_TEMP"] = bash_path(first_temp)
            env["REVIEW_GATE_TEST_MKTEMP_STATE"] = bash_path(allocation_state)
            allocation_stubs = textwrap.dedent(
                """\
                gh() {
                  if [ "$1 $2" = "repo view" ]; then
                    printf '%s\n' 'example/canonical'
                  else
                    return 96
                  fi
                }
                mktemp() {
                  if [ ! -e "$REVIEW_GATE_TEST_MKTEMP_STATE" ]; then
                    : > "$REVIEW_GATE_TEST_MKTEMP_STATE"
                    : > "$REVIEW_GATE_TEST_FIRST_TEMP"
                    printf '%s\n' "$REVIEW_GATE_TEST_FIRST_TEMP"
                    return 0
                  fi
                  return 1
                }
                export -f gh mktemp
                exec bash "$@"
                """
            )
            result = run_with_bash_path(
                [
                    "bash",
                    "-c",
                    allocation_stubs,
                    "review-gate-allocation-test",
                    bash_path(SCRIPT),
                    "462",
                    SHA,
                ],
                stub_directory=directory,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(
                "ERROR: cannot allocate temporary PR identity input",
                result.stderr,
            )
            self.assertFalse(first_temp.exists(), "first allocated temp must be removed")

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

    def test_current_api_commit_id_cannot_rewrite_a_stale_explicit_head_binding(self):
        result = self.run_gate([
            self.review(commit_id=SHA, head_marker=STALE_SHA),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("**HEAD reviewed:** must name exact head", result.stdout)

    def test_later_unbound_review_revokes_an_earlier_bound_acceptance(self):
        result = self.run_gate([
            self.review(at="2026-01-01T00:00:00Z"),
            self.review(
                commit_id=SHA,
                head_marker=STALE_SHA,
                at="2026-01-01T00:01:00Z",
            ),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("**HEAD reviewed:** must name exact head", result.stdout)

    def test_missing_explicit_head_binding_is_not_an_acceptance(self):
        result = self.run_gate([
            self.review(commit_id=SHA, bind_head=False),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("**HEAD reviewed:** must name exact head", result.stdout)

    def test_moved_head_refuses_a_stale_review_event(self):
        result = self.run_gate([self.review()], head_sha=STALE_SHA)

        self.assertEqual(result.returncode, 1)
        self.assertIn("reviewed SHA is not the current PR head", result.stdout)

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

    def test_acceptance_carries_across_a_clean_two_parent_main_merge(self):
        directory, repository, accepted, base, head = self.clean_merge_history()
        self.addCleanup(directory.cleanup)
        result = self.run_carry_gate(repository, accepted, base, head)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            f"carried from accepted first parent {accepted}", result.stdout
        )

    def test_carry_forward_refuses_an_extra_tree_change_in_the_merge_commit(self):
        directory, repository, accepted, base, _head = self.clean_merge_history()
        self.addCleanup(directory.cleanup)
        head = self.commit_file(repository, "ride-along.txt", "extra\n", "extra")
        tree = self.git(repository, "rev-parse", "HEAD^{tree}").stdout.strip()
        authored_merge = self.git(
            repository,
            "commit-tree", tree, "-p", accepted, "-p", base,
            input="authored merge\n",
        ).stdout.strip()
        result = self.run_carry_gate(
            repository, accepted, base, authored_merge
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_carry_forward_refuses_wrong_parent_order(self):
        directory, repository, accepted, base, head = self.clean_merge_history()
        self.addCleanup(directory.cleanup)
        tree = self.git(repository, "rev-parse", f"{head}^{{tree}}").stdout.strip()
        reversed_merge = self.git(
            repository,
            "commit-tree", tree, "-p", base, "-p", accepted,
            input="reversed merge\n",
        ).stdout.strip()
        result = self.run_carry_gate(
            repository, accepted, base, reversed_merge
        )

        self.assertEqual(result.returncode, 1)

    def test_carry_forward_refuses_a_second_parent_outside_base_history(self):
        directory, repository, accepted, base, _head = self.clean_merge_history()
        self.addCleanup(directory.cleanup)
        self.git(repository, "switch", "-q", "--detach", accepted)
        rogue = self.commit_file(repository, "rogue.txt", "rogue\n", "rogue")
        self.git(repository, "switch", "-q", "feature")
        self.git(repository, "reset", "--hard", accepted)
        self.git(repository, "merge", "--no-ff", "-qm", "merge rogue", rogue)
        head = self.git(repository, "rev-parse", "HEAD").stdout.strip()
        result = self.run_carry_gate(repository, accepted, base, head)

        self.assertEqual(result.returncode, 1)

    def test_carry_forward_refuses_a_manually_resolved_conflict(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repository = Path(directory.name)
        self.git(repository, "init", "-q", "-b", "main")
        self.commit_file(repository, "conflict.txt", "base\n", "base")
        self.git(repository, "switch", "-qc", "feature")
        accepted = self.commit_file(repository, "conflict.txt", "feature\n", "feature")
        self.git(repository, "switch", "-q", "main")
        base = self.commit_file(repository, "conflict.txt", "main\n", "main")
        self.git(repository, "switch", "-q", "feature")
        self.git(repository, "merge", "--no-ff", "main", check=False)
        head = self.commit_file(repository, "conflict.txt", "resolved\n", "resolve")
        result = self.run_carry_gate(repository, accepted, base, head)

        self.assertEqual(result.returncode, 1)

    def test_later_changes_required_on_carried_parent_supersedes_acceptance(self):
        directory, repository, accepted, base, head = self.clean_merge_history()
        self.addCleanup(directory.cleanup)
        reviews = [
            self.review(
                commit_id=accepted,
                head_marker=accepted,
                at="2026-01-01T00:00:00Z",
            ),
            self.review(
                commit_id=accepted,
                head_marker=accepted,
                body="**From:** reviewer\n\n**Verdict:** changes required",
                at="2026-01-01T00:01:00Z",
            ),
        ]

        result = self.run_carry_gate(
            repository, accepted, base, head, reviews=reviews
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("changes required by reviewer", result.stdout)

    def test_carry_forward_refuses_an_ambiguous_parent_verdict(self):
        directory, repository, accepted, base, head = self.clean_merge_history()
        self.addCleanup(directory.cleanup)
        reviews = [self.review(
                commit_id=accepted,
                head_marker=accepted,
                body=(
                    "**From:** reviewer\n\n**Verdict:** accept\n\n"
                    "**Verdict:** changes required"
                ),
            )]
        result = self.run_carry_gate(
            repository, accepted, base, head, reviews=reviews
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("rejected for format", result.stdout)

    def test_carry_forward_refuses_a_non_default_target_branch(self):
        directory, repository, accepted, base, head = self.clean_merge_history()
        self.addCleanup(directory.cleanup)
        result = self.run_carry_gate(
            repository, accepted, base, head, base_ref="release"
        )

        self.assertEqual(result.returncode, 1)

    def test_carry_forward_refuses_missing_commit_objects(self):
        result = self.run_gate([
            self.review(commit_id=STALE_SHA, head_marker=STALE_SHA),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("carried from accepted first parent", result.stdout)

    def test_comment_style_verdict_is_rejected(self):
        result = self.run_gate([
            self.review(body="**From:** reviewer\n\nVerdict: accept"),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("rejected for format", result.stdout)

    def test_exact_dependabot_identity_is_an_implicit_author_lane(self):
        result = self.run_gate(
            [self.review()],
            labels=(),
            identity=self.dependabot_identity(),
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("independent exact-head acceptance from reviewer", result.stdout)

    def test_dependabot_lookalikes_do_not_get_the_implicit_lane(self):
        for identity in (
            self.dependabot_identity(user_id=1),
            self.dependabot_identity(login="contributor"),
            self.dependabot_identity(user_type="User"),
            self.dependabot_identity(head_repo_id=200),
            self.dependabot_identity(head_repo_id=None),
        ):
            with self.subTest(identity=identity):
                result = self.run_gate([self.review()], labels=(), identity=identity)

                self.assertEqual(result.returncode, 1)
                self.assertIn("expected exactly one agent:<id> author lane", result.stdout)

    def test_dependabot_cannot_carry_a_human_author_lane(self):
        result = self.run_gate(
            [self.review()],
            labels=("agent:spoofed",),
            identity=self.dependabot_identity(),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("must not carry agent:<id> labels", result.stdout)

    def test_changes_required_still_blocks_dependabot(self):
        result = self.run_gate(
            [self.review(
                state="COMMENTED",
                body="**From:** reviewer\n\n**Verdict:** changes required",
            )],
            labels=(),
            identity=self.dependabot_identity(),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("changes required by reviewer", result.stdout)

    @staticmethod
    def dependabot_identity(
        *,
        user_id=49699333,
        login="dependabot[bot]",
        user_type="Bot",
        head_repo_id=100,
        base_repo_id=100,
    ):
        return {
            "user": {"id": user_id, "login": login, "type": user_type},
            "head": {"repo": {"id": head_repo_id}},
            "base": {"repo": {"id": base_repo_id}},
        }


class GitHubSynonymVerdictTest(GateHarness):
    """GitHub's verdict words count the same as the package's (#70).

    Only the issue's option 1 is implemented: Approve means accept and
    Request changes means changes required, case-insensitively. Trailing
    text still voids the review, and near-misses like `approved` do not
    count — those boundaries are pinned below.
    """

    def test_approve_counts_as_accept(self):
        for word in ("Approve", "approve", "APPROVE"):
            with self.subTest(word=word):
                result = self.run_gate([
                    self.review(body=f"**From:** reviewer\n\n**Verdict:** {word}"),
                ])

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("acceptance from reviewer", result.stdout)

    def test_request_changes_blocks(self):
        for word in ("Request changes", "request changes", "REQUEST CHANGES"):
            with self.subTest(word=word):
                result = self.run_gate([
                    self.review(
                        state="COMMENTED",
                        body=f"**From:** reviewer\n\n**Verdict:** {word}",
                    ),
                ])

                self.assertEqual(result.returncode, 1)
                self.assertIn("changes required by reviewer", result.stdout)

    def test_agreeing_synonyms_do_not_void_each_other(self):
        result = self.run_gate([
            self.review(body="**From:** reviewer\n\n**Verdict:** Approve\n\n**Verdict:** accept"),
        ])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("acceptance from reviewer", result.stdout)

    def test_conflicting_verdict_lines_still_void_the_review(self):
        result = self.run_gate([
            self.review(body="**From:** reviewer\n\n**Verdict:** Approve\n\n**Verdict:** Request changes"),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn("rejected for format", result.stdout)

    def test_synonym_with_trailing_text_is_still_rejected(self):
        result = self.run_gate([
            self.review(body="**From:** reviewer\n\n**Verdict:** Approve, please land this"),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("rejected for format", result.stdout)

    def test_approved_is_not_a_synonym(self):
        result = self.run_gate([
            self.review(body="**From:** reviewer\n\n**Verdict:** approved"),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("rejected for format", result.stdout)


class MalformedFromValueTest(GateHarness):
    """A review whose From value is malformed must be named, never silent.

    The gate drops such reviews before any diagnostic is computed, and for
    COMMENTED reviews — nearly every real verdict — nothing said why.
    """

    def test_prefixed_from_value_is_named_with_its_literal(self):
        result = self.run_gate([
            self.review(body="**From:** agent:some-reviewer\n\n**Verdict:** accept"),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn(
            "WARN: review from agent-account ignored:"
            " **From:** agent:some-reviewer is not a bare lane name",
            result.stdout,
        )

    def test_malformed_from_on_an_approval_keeps_the_unattributed_warning(self):
        # The precise warning covers the COMMENTED shape from the issue;
        # an APPROVED review with a malformed From keeps the older message.
        result = self.run_gate([
            self.review(state="APPROVED", body="**From:** agent:some-reviewer"),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertIn("carries no **From:** marker", result.stdout)
        self.assertNotIn("is not a bare lane name", result.stdout)

    def test_a_review_with_no_from_line_stays_quiet(self):
        result = self.run_gate([
            self.review(body="Just a drive-by comment."),
        ])

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("is not a bare lane name", result.stdout)

    def test_a_valid_from_value_warns_about_nothing_new(self):
        result = self.run_gate([self.review()])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("is not a bare lane name", result.stdout)


class UnattributedApprovalTest(GateHarness):
    """An approval the gate ignores must be named, never silently dropped.

    #55 reported this from belimbing#462: the owner approved at the exact head
    with an empty body and the check produced no output about it at all. The
    approval not counting is the design working; its being invisible was not.
    """

    def approval(self, **kwargs):
        """A bare UI approval — no body, no markers."""
        return self.review(state="APPROVED", body="", bind_head=False, **kwargs)

    def test_marker_less_approval_is_named_not_silent(self):
        result = self.run_gate([self.approval(
            user={"id": 4242, "login": "owner-account", "type": "User"})])
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "WARN: an APPROVED review from owner-account was ignored", result.stdout
        )
        self.assertIn("no independent exact-head acceptance", result.stdout)

    def test_a_marked_acceptance_still_clears_alongside_one(self):
        result = self.run_gate([
            self.approval(user={"id": 4242, "login": "owner-account", "type": "User"}),
            self.review(agent="reviewer", state="APPROVED"),
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("acceptance from reviewer", result.stdout)


class IssueCommentVerdictTest(GateHarness):
    """Verdicts posted in issue comments must warn loudly and never count (#71).

    The gate reads pull request reviews only. An exact-head verdict in an issue
    comment must emit a WARN naming the agent, and the FAIL message must name
    the pull request review surface.
    """

    def test_verdict_in_issue_comment_warns_and_fails_the_gate(self):
        result = self.run_gate(
            reviews=[],
            comments=[{
                "body": f"**From:** reviewer\n\n**Verdict:** accept\n\n**HEAD reviewed:** `{SHA}`",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn(
            "WARN: a verdict from reviewer was found in an issue comment; the gate reads pull request reviews only",
            result.stdout,
        )
        self.assertIn("require a pull request review with", result.stdout)

    def test_blocking_verdict_in_issue_comment_warns_and_fails_the_gate(self):
        result = self.run_gate(
            reviews=[],
            comments=[{
                "body": "**From:** someone-else\n\n**Verdict:** changes required",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no independent exact-head acceptance", result.stdout)
        self.assertIn(
            "WARN: a verdict from someone-else was found in an issue comment; the gate reads pull request reviews only",
            result.stdout,
        )

    def test_ordinary_issue_comment_stays_quiet(self):
        result = self.run_gate(
            reviews=[],
            comments=[{
                "body": "Just asking a question about the design.",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("was found in an issue comment", result.stdout)

    def test_comment_with_from_but_no_verdict_stays_quiet(self):
        result = self.run_gate(
            reviews=[],
            comments=[{
                "body": "**From:** reviewer\n\nLooks good to me in general.",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("was found in an issue comment", result.stdout)

    def test_comment_with_verdict_but_no_from_stays_quiet(self):
        result = self.run_gate(
            reviews=[],
            comments=[{
                "body": "**Verdict:** accept",
            }],
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("was found in an issue comment", result.stdout)

    def test_multiple_comments_from_same_agent_are_deduplicated(self):
        result = self.run_gate(
            reviews=[],
            comments=[
                {"body": "**From:** reviewer\n\n**Verdict:** accept"},
                {"body": "**From:** reviewer\n\n**Verdict:** accept"},
            ],
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.count(
                "WARN: a verdict from reviewer was found in an issue comment; the gate reads pull request reviews only"
            ),
            1,
        )

    def test_fail_message_names_the_pull_request_review_surface(self):
        result = self.run_gate(reviews=[])
        self.assertEqual(result.returncode, 1)
        self.assertIn("require a pull request review with", result.stdout)


if __name__ == "__main__":
    unittest.main()
