import os
import stat
import unittest
from pathlib import Path

import test_orient_steward


class ProjectHookResolutionTest(unittest.TestCase):
    """#8: the project hook lives outside the mount, at .ai-team/ in the
    adopting repository's root (or AI_TEAM_PROJECT_ORIENT), never inside
    docs/ai-team/ itself — so the mounted package stays byte-identical to
    upstream and `git subtree pull` can never conflict on it."""

    setUp = test_orient_steward.OrientStewardMechanismTest.setUp
    tearDown = test_orient_steward.OrientStewardMechanismTest.tearDown
    write_gh_stub = test_orient_steward.OrientStewardMechanismTest.write_gh_stub

    def run_orient(self, env_extra=None):
        env = os.environ.copy()
        env.update(
            ORIENT_TEST_STEWARDS="",
            PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
        )
        env.update(env_extra or {})
        from _test_support import run_with_bash_path

        return run_with_bash_path(
            ["bash", str(self.scripts / "orient.sh")],
            stub_directory=self.bin,
            cwd=self.base,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def write_hook(self, path: Path, marker: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/usr/bin/env bash\necho "{marker}"\n', encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def test_no_hook_prints_a_discoverable_note_rather_than_nothing(self):
        result = self.run_orient()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "note: no project hook at .ai-team/project-orient.sh (see docs/ai-team/templates/)",
            result.stdout,
        )

    def test_a_hook_at_dot_ai_team_in_the_repository_root_runs(self):
        self.write_hook(self.base / ".ai-team" / "project-orient.sh", "PROJECT-HOOK-RAN")
        result = self.run_orient()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PROJECT-HOOK-RAN", result.stdout)
        self.assertNotIn("note: no project hook", result.stdout)

    def test_a_hook_inside_the_mounted_scripts_directory_is_never_run(self):
        # The regression #8 exists to prevent: a copy left at the old location
        # (scripts/project-orient.sh, inside what git subtree pull manages)
        # must not be picked up silently — that would be the exact vendored
        # drift this fix removes.
        self.write_hook(self.scripts / "project-orient.sh", "WRONG-LOCATION-RAN")
        result = self.run_orient()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("WRONG-LOCATION-RAN", result.stdout)
        self.assertIn("note: no project hook", result.stdout)

    def test_ai_team_project_orient_override_wins_over_the_default_location(self):
        elsewhere = self.base / "elsewhere" / "hook.sh"
        self.write_hook(elsewhere, "OVERRIDE-HOOK-RAN")
        self.write_hook(self.base / ".ai-team" / "project-orient.sh", "DEFAULT-HOOK-RAN")

        result = self.run_orient({"AI_TEAM_PROJECT_ORIENT": str(elsewhere)})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OVERRIDE-HOOK-RAN", result.stdout)
        self.assertNotIn("DEFAULT-HOOK-RAN", result.stdout)

    def test_orient_exports_the_resolved_default_branch_for_the_hook(self):
        # The hook has no relative path back to scripts/_default_branch.sh
        # once copied out to .ai-team/, so it depends on this instead.
        self.write_hook(
            self.base / ".ai-team" / "project-orient.sh",
            "BASE-IS-$AI_TEAM_DEFAULT_BRANCH",
        )
        result = self.run_orient()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BASE-IS-main", result.stdout)


if __name__ == "__main__":
    unittest.main()
