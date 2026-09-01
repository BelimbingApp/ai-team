import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path


SCRIPT = Path(__file__).with_name("subtree_pull_gate.sh")
PREFIX = "docs/ai-team"


class SubtreePullGateTest(unittest.TestCase):
    """#61: a pure, verifiable subtree pull is a trusted shape needing no
    adopter-side review; anything else — including 'cannot judge' — falls
    back to the ordinary review requirement. Exit 0 / 1 / 2 respectively."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)

        # The "upstream" package repository: package-mount is simulated by a
        # branch whose root tree IS the package content.
        self.upstream = base / "upstream"
        self.upstream.mkdir()
        self.git(self.upstream, "init", "-q", "-b", "package-mount")
        (self.upstream / "scripts").mkdir()
        (self.upstream / "scripts" / "tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.upstream / "templates").mkdir()
        (self.upstream / "templates" / "independent-review.yml").write_text(
            "# template header\n# second line\non: x\nbody: same\n", encoding="utf-8"
        )
        self.git(self.upstream, "add", "-A")
        self.git(self.upstream, "commit", "-qm", "package v1")
        self.upstream_v1_tree = self.git_out(self.upstream, "rev-parse", "HEAD^{tree}")

        # The adopter repository with a mount whose tree matches upstream v1.
        self.adopter = base / "adopter"
        self.adopter.mkdir()
        self.git(self.adopter, "init", "-q", "-b", "main")
        (self.adopter / "app.txt").write_text("app\n", encoding="utf-8")
        self.git(self.adopter, "add", "-A")
        self.git(self.adopter, "commit", "-qm", "initial")
        self.base_sha = self.git_out(self.adopter, "rev-parse", "HEAD")

    def tearDown(self):
        self.dir.cleanup()

    def git(self, cwd, *args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=cwd, check=True, capture_output=True, text=True,
        )

    def git_out(self, cwd, *args) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit_pull(self, message_prefix=True):
        """Simulate `git subtree pull --squash`: a squash commit whose
        subtree matches upstream, then a merge carrying it."""
        squash_message = (
            f"Squashed '{PREFIX}/' changes from aaa..bbb" if message_prefix
            else "some ordinary commit"
        )
        self.git(self.adopter, "checkout", "-q", "-b", "pull-branch")
        mount = self.adopter / PREFIX
        (mount / "scripts").mkdir(parents=True)
        (mount / "scripts" / "tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (mount / "templates").mkdir()
        (mount / "templates" / "independent-review.yml").write_text(
            "# template header\n# second line\non: x\nbody: same\n", encoding="utf-8"
        )
        self.git(self.adopter, "add", "-A")
        self.git(self.adopter, "commit", "-qm", squash_message)
        self.git(self.adopter, "checkout", "-q", "main")
        self.git(
            self.adopter, "merge", "--no-ff", "-q", "pull-branch",
            "-m", "chore: pull ai-team package-mount",
        )
        return self.git_out(self.adopter, "rev-parse", "HEAD")

    def run_gate(self, base, head, upstream_branch="package-mount"):
        env = os.environ.copy()
        env["AI_TEAM_TEST_UPSTREAM_URL"] = f"file://{self.upstream}"
        return run_with_bash_path(
            ["bash", bash_path(SCRIPT), base, head, PREFIX,
             "Example/ai-team", upstream_branch],
            stub_directory=Path(self.dir.name),
            cwd=self.adopter,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_a_faithful_pull_is_the_trusted_shape(self):
        head = self.commit_pull()
        result = self.run_gate(self.base_sha, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("trusted pull shape verified", result.stdout)

    def test_a_direct_mount_edit_is_not_trusted(self):
        self.git(self.adopter, "checkout", "-q", "-b", "edit-branch")
        mount = self.adopter / PREFIX / "scripts"
        mount.mkdir(parents=True)
        (mount / "tool.sh").write_text("#!/bin/sh\nhand edit\n", encoding="utf-8")
        self.git(self.adopter, "add", "-A")
        self.git(self.adopter, "commit", "-qm", "feat: edit the mount directly")
        head = self.git_out(self.adopter, "rev-parse", "HEAD")

        result = self.run_gate(self.base_sha, head)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("outside the subtree-pull shape", result.stderr)

    def test_a_pull_shaped_commit_with_a_foreign_tree_is_not_trusted(self):
        # Squash-shaped message, but the content matches nothing upstream:
        # the shape alone must never grant the exemption.
        head = self.commit_pull()
        self.git(self.adopter, "checkout", "-q", "-b", "poison")
        tool = self.adopter / PREFIX / "scripts" / "tool.sh"
        tool.write_text("#!/bin/sh\npoisoned\n", encoding="utf-8")
        self.git(self.adopter, "add", "-A")
        self.git(self.adopter, "commit", "-qm", f"Squashed '{PREFIX}/' changes from bbb..ccc")
        poisoned = self.git_out(self.adopter, "rev-parse", "HEAD")

        result = self.run_gate(self.base_sha, poisoned)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("matches no commit", result.stderr)

    def test_a_template_regenerated_workflow_rides_along(self):
        head = self.commit_pull()
        self.git(self.adopter, "checkout", "-q", "main")
        workflows = self.adopter / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ai-team-independent-review.yml").write_text(
            "# adopter header, freely worded\non: x\nbody: same\n", encoding="utf-8"
        )
        self.git(self.adopter, "add", "-A")
        self.git(self.adopter, "commit", "-qm", "chore: reinstall workflow from template")
        head = self.git_out(self.adopter, "rev-parse", "HEAD")

        result = self.run_gate(self.base_sha, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_deviating_workflow_ends_the_exemption(self):
        head = self.commit_pull()
        self.git(self.adopter, "checkout", "-q", "main")
        workflows = self.adopter / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ai-team-independent-review.yml").write_text(
            "# adopter header\non: x\nbody: DIFFERENT\n", encoding="utf-8"
        )
        self.git(self.adopter, "add", "-A")
        self.git(self.adopter, "commit", "-qm", "chore: deviate")
        head = self.git_out(self.adopter, "rev-parse", "HEAD")

        result = self.run_gate(self.base_sha, head)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("deviates from its pulled template", result.stderr)

    def test_any_other_non_mount_change_ends_the_exemption(self):
        head = self.commit_pull()
        self.git(self.adopter, "checkout", "-q", "main")
        (self.adopter / "app.txt").write_text("changed\n", encoding="utf-8")
        self.git(self.adopter, "add", "-A")
        self.git(self.adopter, "commit", "-qm", "feat: ride-along app change")
        head = self.git_out(self.adopter, "rev-parse", "HEAD")

        result = self.run_gate(self.base_sha, head)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("outside the template-workflow allowance", result.stderr)

    def test_a_range_without_mount_changes_is_not_the_pull_shape(self):
        self.git(self.adopter, "checkout", "-q", "-b", "plain")
        (self.adopter / "app.txt").write_text("changed\n", encoding="utf-8")
        self.git(self.adopter, "add", "-A")
        self.git(self.adopter, "commit", "-qm", "feat: ordinary change")
        head = self.git_out(self.adopter, "rev-parse", "HEAD")

        result = self.run_gate(self.base_sha, head)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("only for pulls", result.stderr)

    def test_missing_objects_cannot_be_judged(self):
        head = self.commit_pull()
        result = self.run_gate("f" * 40, head)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot judge", result.stderr)

    def test_an_unfetchable_upstream_cannot_be_judged(self):
        head = self.commit_pull()
        result = self.run_gate(self.base_sha, head, upstream_branch="missing-branch")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cannot judge", result.stderr)


if __name__ == "__main__":
    unittest.main()
