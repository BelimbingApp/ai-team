import subprocess
import unittest

import test_orient_steward


class OrientMainFreshnessTest(unittest.TestCase):
    """orient.sh must report checkout freshness on main as well as lanes."""

    write_gh_stub = test_orient_steward.OrientStewardMechanismTest.write_gh_stub

    def setUp(self):
        # Reuse the assembled orient/gh fixture rather than maintaining a
        # second copy of that mechanism. Add only the real Git topology this
        # contract needs: a checkout whose origin has advanced independently.
        test_orient_steward.OrientStewardMechanismTest.setUp(self)
        self.configure_identity(self.base)
        (self.base / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=self.base, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.base, check=True)

        self.remote = self.base / "remote.git"
        self.publisher = self.base / "publisher"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.base, check=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=self.base, check=True)

        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.remote), str(self.publisher)], check=True)
        self.configure_identity(self.publisher)
        (self.publisher / "remote-only.txt").write_text("new on origin\n", encoding="utf-8")
        subprocess.run(["git", "add", "remote-only.txt"], cwd=self.publisher, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance origin"], cwd=self.publisher, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.publisher, check=True)
        subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=self.base, check=True)

    def tearDown(self):
        test_orient_steward.OrientStewardMechanismTest.tearDown(self)

    @staticmethod
    def configure_identity(path) -> None:
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)

    def run_orient(self):
        return test_orient_steward.OrientStewardMechanismTest.run_orient(self, "")

    def test_a_deliberately_behind_main_checkout_reports_the_exact_gap(self):
        behind = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            cwd=self.base,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(behind, "1", "fixture must be genuinely behind before orient runs")

        result = self.run_orient()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "main: BEHIND origin/main by 1 commit — scripts and files you read here are stale",
            result.stdout,
        )

    def test_a_current_main_checkout_reports_that_it_contains_origin_main(self):
        subprocess.run(["git", "merge", "-q", "--ff-only", "origin/main"], cwd=self.base, check=True)

        result = self.run_orient()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("main: contains origin/main", result.stdout)
        self.assertNotIn("BEHIND origin/main", result.stdout)

    def test_a_behind_lane_keeps_the_existing_instruction(self):
        subprocess.run(["git", "switch", "-q", "-c", "agent/test-lane"], cwd=self.base, check=True)

        result = self.run_orient()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "agent/test-lane: BEHIND origin/main — merge it in before you ask anyone to review",
            result.stdout,
        )
        self.assertNotIn("scripts and files you read here are stale", result.stdout)


if __name__ == "__main__":
    unittest.main()
