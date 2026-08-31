import base64
import hashlib
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import _bash_executable, bash_path


SCRIPT_DIRECTORY = Path(__file__).parent.resolve()
ROOT = Path(
    subprocess.run(
        ["git", "-C", str(SCRIPT_DIRECTORY), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
).resolve()
PACKAGE_DIRECTORY = SCRIPT_DIRECTORY.parent
PACKAGE_PATHSPEC = PACKAGE_DIRECTORY.relative_to(ROOT).as_posix()
PACKAGE_WORKFLOW = ROOT / ".github" / "workflows" / "independent-review.yml"
ADOPTER_TEMPLATE = PACKAGE_DIRECTORY / "templates" / "independent-review.yml"
WORKFLOW_SHA = "c" * 40
REPOSITORY = "example/project"
VALID_SCRIPT = b"#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n"


def run_block(workflow: str, step_name: str) -> str:
    """Extract one literal `run: |` block without adding a YAML dependency."""
    lines = workflow.splitlines()
    marker = f"- name: {step_name}"
    step = next(i for i, line in enumerate(lines) if line.strip() == marker)
    run = next(i for i in range(step + 1, len(lines)) if lines[i].strip() == "run: |")
    run_indent = len(lines[run]) - len(lines[run].lstrip())
    end = len(lines)
    for i in range(run + 1, len(lines)):
        stripped = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip())
        if stripped and indent <= run_indent:
            end = i
            break
    return textwrap.dedent("\n".join(lines[run + 1 : end])) + "\n"


def blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def contents_response(canonical_path: str, script_content: bytes = VALID_SCRIPT, **overrides) -> str:
    data = {
        "type": "file",
        "path": canonical_path,
        "encoding": "base64",
        "content": base64.b64encode(script_content).decode("ascii"),
        "size": len(script_content),
        "sha": blob_sha(script_content),
    }
    data.update(overrides)
    return json.dumps(data)


class IndependentReviewWorkflowTest(unittest.TestCase):
    def workflows(self):
        workflows = [("adopter", ADOPTER_TEMPLATE)]
        if PACKAGE_PATHSPEC == "package":
            workflows.append(("package", PACKAGE_WORKFLOW))
        return workflows

    def run_materializer(
        self,
        workflow_path: Path,
        gate_path: str,
        response: str,
        *,
        gh_exit: int = 0,
        repository: str = REPOSITORY,
        workflow_sha: str = WORKFLOW_SHA,
    ):
        workflow = workflow_path.read_text(encoding="utf-8")
        script = run_block(workflow, "Materialize trusted review grammar")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp = root / "runner"
            runner_temp.mkdir()
            response_file = root / "response.json"
            response_file.write_text(response, encoding="utf-8")
            calls_file = root / "gh-calls.txt"

            gh = root / "gh"
            gh.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" > "$GH_CALLS"
if [ "$GH_EXIT" -ne 0 ]; then
  echo "simulated API failure (HTTP 404)" >&2
  exit "$GH_EXIT"
fi
cat "$CONTENTS_RESPONSE"
""",
                encoding="utf-8",
                newline="\n",
            )
            gh.chmod(0o755)

            env = os.environ.copy()
            env["AI_TEAM_TEST_STUB_PATH"] = bash_path(root)
            env["CONTENTS_RESPONSE"] = bash_path(response_file)
            env["GH_CALLS"] = bash_path(calls_file)
            env["GH_EXIT"] = str(gh_exit)
            env["GH_TOKEN"] = "fixture-token"
            env["REVIEW_GATE_REPOSITORY"] = repository
            env["REVIEW_GATE_WORKFLOW_SHA"] = workflow_sha
            env["REVIEW_GATE_PATH"] = gate_path
            env["RUNNER_TEMP"] = bash_path(runner_temp)
            result = subprocess.run(
                [
                    _bash_executable(),
                    "-c",
                    'PATH="$AI_TEAM_TEST_STUB_PATH:$PATH"; export PATH\n' + script,
                ],
                cwd=root,
                env=env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            materialized = runner_temp / "review_gate.sh"
            content = materialized.read_bytes() if materialized.is_file() else None
            calls = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.is_file() else []

        return result, content, calls

    def test_canonical_workflows_use_only_pull_request_target(self):
        for name, path in self.workflows():
            with self.subTest(workflow=name):
                workflow = path.read_text(encoding="utf-8")
                self.assertIn("pull_request_target:", workflow)
                self.assertNotIn("pull_request_review:", workflow)
                self.assertNotIn("  pull_request:\n", workflow)
                self.assertNotIn("actions/checkout@", workflow)
                self.assertNotIn("present=false", workflow)
                self.assertNotIn("default_branch", workflow)

    def test_workflows_pin_the_contents_request_and_quote_event_values(self):
        for name, path in self.workflows():
            with self.subTest(workflow=name):
                workflow = path.read_text(encoding="utf-8")
                self.assertIn("REVIEW_GATE_REPOSITORY: ${{ github.repository }}", workflow)
                self.assertIn("REVIEW_GATE_WORKFLOW_SHA: ${{ github.workflow_sha }}", workflow)
                self.assertIn("REVIEW_GATE_PR_NUMBER: ${{ github.event.pull_request.number }}", workflow)
                self.assertIn("REVIEW_GATE_HEAD_SHA: ${{ github.event.pull_request.head.sha }}", workflow)
                self.assertIn("gh api --method GET", workflow)
                self.assertIn('-f "ref=$REVIEW_GATE_WORKFLOW_SHA"', workflow)
                self.assertIn('"$RUNNER_TEMP/review_gate.sh" "$REVIEW_GATE_PR_NUMBER" "$REVIEW_GATE_HEAD_SHA"', workflow)
                self.assertNotIn("${{", run_block(workflow, "Materialize trusted review grammar"))
                self.assertNotIn("${{", run_block(workflow, "Verify independent review of this exact head"))

    def test_source_and_adopter_paths_are_exact(self):
        adopter = ADOPTER_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("REVIEW_GATE_PATH: docs/ai-team/scripts/review_gate.sh", adopter)
        self.assertNotIn("REVIEW_GATE_PATH: package/scripts/review_gate.sh", adopter)

        if PACKAGE_PATHSPEC != "package":
            self.skipTest("the source repository workflow is outside an adopter mount")
        package = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("REVIEW_GATE_PATH: package/scripts/review_gate.sh", package)
        self.assertNotIn("REVIEW_GATE_PATH: docs/ai-team/scripts/review_gate.sh", package)

    def test_materializer_fetches_and_verifies_the_exact_blob(self):
        cases = [("adopter", ADOPTER_TEMPLATE, "docs/ai-team/scripts/review_gate.sh")]
        if PACKAGE_PATHSPEC == "package":
            cases.append(("package", PACKAGE_WORKFLOW, "package/scripts/review_gate.sh"))

        for name, workflow, gate_path in cases:
            with self.subTest(workflow=name):
                result, content, calls = self.run_materializer(
                    workflow,
                    gate_path,
                    contents_response(gate_path),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(content, VALID_SCRIPT)
                self.assertIn("--method", calls)
                self.assertIn("GET", calls)
                self.assertIn(f"repos/{REPOSITORY}/contents/{gate_path}", calls)
                self.assertIn(f"ref={WORKFLOW_SHA}", calls)

    def test_materializer_fails_closed_on_api_or_payload_errors(self):
        path = "docs/ai-team/scripts/review_gate.sh"
        malformed = [
            ("404", contents_response(path), 1, REPOSITORY, WORKFLOW_SHA),
            ("malformed-json", "{", 0, REPOSITORY, WORKFLOW_SHA),
            ("array", "[]", 0, REPOSITORY, WORKFLOW_SHA),
            ("wrong-type", contents_response(path, type="dir"), 0, REPOSITORY, WORKFLOW_SHA),
            ("wrong-path", contents_response(path, path="wrong/review_gate.sh"), 0, REPOSITORY, WORKFLOW_SHA),
            ("wrong-encoding", contents_response(path, encoding="none"), 0, REPOSITORY, WORKFLOW_SHA),
            ("empty-content", contents_response(path, content=""), 0, REPOSITORY, WORKFLOW_SHA),
            ("invalid-base64", contents_response(path, content="%%%"), 0, REPOSITORY, WORKFLOW_SHA),
            ("size-mismatch", contents_response(path, size=len(VALID_SCRIPT) + 1), 0, REPOSITORY, WORKFLOW_SHA),
            ("blob-mismatch", contents_response(path, sha="d" * 40), 0, REPOSITORY, WORKFLOW_SHA),
            ("missing-shebang", contents_response(path, b"exit 0\n"), 0, REPOSITORY, WORKFLOW_SHA),
            ("invalid-bash", contents_response(path, b"#!/usr/bin/env bash\nif\n"), 0, REPOSITORY, WORKFLOW_SHA),
            ("invalid-repository", contents_response(path), 0, "example/project/extra", WORKFLOW_SHA),
            ("invalid-workflow-sha", contents_response(path), 0, REPOSITORY, "main"),
        ]

        for name, response, gh_exit, repository, workflow_sha in malformed:
            with self.subTest(case=name):
                result, _, _ = self.run_materializer(
                    ADOPTER_TEMPLATE,
                    path,
                    response,
                    gh_exit=gh_exit,
                    repository=repository,
                    workflow_sha=workflow_sha,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
