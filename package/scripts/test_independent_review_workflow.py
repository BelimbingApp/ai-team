import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from _test_support import _bash_executable


SCRIPT_DIRECTORY = Path(__file__).parent.resolve()
# `Path(__file__).parents[2]` reached the repository root only by coincidence
# in the home layout (scripts -> package -> root, two hops). Mounted, the
# package root sits one hop deeper (scripts -> ai-team -> docs -> root, three
# hops) — a fixed parent count cannot reach "the repository root" from a
# depth that differs between the two contexts (#26 review, codex-fasttrack:
# this resolved to <adopter>/docs and errored trying to open
# <adopter>/docs/.github/workflows/independent-review.yml).
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


class IndependentReviewWorkflowTest(unittest.TestCase):
    """Compares this package's own real CI workflow against the template it
    ships adopters — a self-consistency check for the repository that owns
    both files, meaningful only in the source repository. `.github/workflows/`
    at a repository's own root is never part of what a mount carries (#26),
    so an adopter's own root `.github/workflows/independent-review.yml`, if
    they installed one, is *their* copy of the adopter template — asserting
    it still contains this repository's own `package/scripts/...` form would
    be wrong, not merely inapplicable, for exactly the repositories where the
    file happens to exist. `PACKAGE_PATHSPEC == "package"` is this package's
    actual, fixed name for its own directory; nothing but the source
    repository can be running from a directory with that name at that depth,
    so it is a sound (if convention-based, matching how every other path in
    this test suite already works) way to gate a check with no meaning once
    mounted, without erroring on the file this test never expects to find
    outside its own repository."""

    def setUp(self):
        if PACKAGE_PATHSPEC != "package":
            self.skipTest(
                f"running from '{PACKAGE_PATHSPEC}', not the source repository's 'package' — "
                "this self-consistency check has no meaning once mounted"
            )

    def test_package_and_adopter_workflows_share_the_trusted_shape(self):
        package = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
        adopter = ADOPTER_TEMPLATE.read_text(encoding="utf-8")

        for workflow in (package, adopter):
            self.assertIn("pull_request_target:", workflow)
            self.assertIn("pull_request_review:", workflow)
            self.assertNotIn("  pull_request:\n", workflow)
            self.assertIn("actions/checkout@v5", workflow)
            self.assertIn("ref: ${{ github.event.repository.default_branch }}", workflow)
            self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
            self.assertIn("github.event.pull_request.number", workflow)
            self.assertIn("github.event.pull_request.head.sha", workflow)
            self.assertIn("id: resolve", workflow)
            self.assertIn("steps.resolve.outputs.present == 'true'", workflow)
            self.assertIn("steps.resolve.outputs.present == 'false'", workflow)
            self.assertIn('echo "present=true" >> "$GITHUB_OUTPUT"', workflow)
            self.assertIn('echo "present=false" >> "$GITHUB_OUTPUT"', workflow)

        self.assertIn('run: docs/ai-team/scripts/review_gate.sh', adopter)
        self.assertIn("if [ -x docs/ai-team/scripts/review_gate.sh ]; then", adopter)

        # The package's own root workflow — never the adopter template — must
        # also fall back to the pre-relocation path (#26 review,
        # codex-fasttrack/codex-gpt-5-b): trusted main still has a working
        # grammar at scripts/review_gate.sh until this PR merges, so checking
        # only the new path made the required check vacuously pass, evaluating
        # nothing, for every PR in that window including the one that landed
        # the relocation. An adopter mounting fresh never has an old path to
        # fall back to, so its template correctly keeps only the single check.
        self.assertIn('run: ${{ steps.resolve.outputs.path }}', package)
        self.assertIn("if [ -x package/scripts/review_gate.sh ]; then", package)
        self.assertIn("elif [ -x scripts/review_gate.sh ]; then", package)
        self.assertIn('echo "path=package/scripts/review_gate.sh" >> "$GITHUB_OUTPUT"', package)
        self.assertIn('echo "path=scripts/review_gate.sh" >> "$GITHUB_OUTPUT"', package)

    def test_the_package_workflow_finds_a_legacy_only_grammar_on_the_default_branch(self):
        # A direct regression for the exact failure mode reported: simulate
        # "trusted main has the grammar only at the pre-relocation path" by
        # resolving from a scratch directory that has scripts/review_gate.sh
        # but not package/scripts/review_gate.sh, and confirm the workflow's
        # own resolution logic would find it rather than reporting absent.
        workflow = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
        resolve_step = workflow.split("Resolve trusted review grammar")[1].split("- name:")[0]
        script = resolve_step.split("run: |", 1)[1]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "scripts"
            legacy.mkdir()
            (legacy / "review_gate.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (legacy / "review_gate.sh").chmod(0o755)

            outputs_file = root / "github_output.txt"
            outputs_file.write_text("", encoding="utf-8")

            env = os.environ.copy()
            env["GITHUB_OUTPUT"] = str(outputs_file)
            result = subprocess.run(
                [_bash_executable(), "-c", "set -u\n" + script],
                cwd=str(root),
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = outputs_file.read_text(encoding="utf-8")

        self.assertIn("present=true", outputs)
        self.assertIn("path=scripts/review_gate.sh", outputs)


if __name__ == "__main__":
    unittest.main()
