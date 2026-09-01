import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from _test_support import bash_path, run_with_bash_path

ORIENT = Path(__file__).with_name("orient.sh")
SCRIPT_NAMES = (
    "orient.sh",
    "_lane_issue.sh",
    "_default_branch.sh",
    "halt_status.sh",
    "board.sh",
    "label_hygiene.sh",
    "decide.sh",
)


class OrientDeliberationsTest(unittest.TestCase):
    """orient.sh must surface an open decide.sh proposal, never as 'waiting for owner'."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.base = base
        self.scripts = base / "scripts"
        self.scripts.mkdir()
        for name in SCRIPT_NAMES:
            source = ORIENT.with_name(name)
            destination = self.scripts / name
            shutil.copy2(source, destination)
            destination.chmod(destination.stat().st_mode | stat.S_IXUSR)

        self.bin = base / "bin"
        self.bin.mkdir()
        self.comments = base / "comments.json"
        self.comments.write_text("[]", encoding="utf-8")
        self.counter = base / "counter.txt"
        subprocess.run(["git", "init", "-q", "-b", "main", str(base)], check=True)
        self.write_gh_stub()

    def tearDown(self):
        self.dir.cleanup()

    def write_gh_stub(self):
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                case "$1 $2" in
                  "repo view")
                    if [[ "$*" == *defaultBranchRef* ]]; then printf 'main\\n'
                    else printf 'example/canonical\\n'; fi
                    ;;
                  "issue list")
                    if [[ "$*" == *"--label ops:halt"* ]]; then
                      printf ''
                    elif [[ "$*" == *"--label ops:steward"* ]]; then
                      printf ''
                    elif [[ "$*" == *"--json number,labels --jq"* ]]; then
                      # orient's deliberation-lane scan and decide.sh's own
                      # active_agents(): gh applies --jq itself, so the stub
                      # must hand back the already-filtered issue numbers /
                      # agent ids, not the raw label objects.
                      if [[ "$*" == *"ltrimstr"* ]]; then
                        jq -r '.[]' "$ORIENT_TEST_AGENTS"
                      else
                        jq -r --slurpfile agents "$ORIENT_TEST_AGENTS" \\
                          'if ($agents[0] | length) > 0 then "900" else empty end' <<<'null'
                      fi
                    elif [[ "$*" == *"--json number,labels"* ]]; then
                      # reachability section: gh returns the raw array and
                      # orient.sh runs its own jq downstream.
                      jq -n --slurpfile agents "$ORIENT_TEST_AGENTS" \\
                        '[$agents[0][] | {number: 900, labels: [{name: ("agent:" + .)}]}]'
                    else
                      printf '[]\\n'
                    fi
                    ;;
                  "issue view")
                    jq -n --slurpfile c "$ORIENT_TEST_COMMENTS" '{comments: $c[0]}'
                    ;;
                  "issue comment")
                    body=$(cat)
                    n=$(( $(cat "$ORIENT_TEST_COUNTER" 2>/dev/null || echo 0) + 1 ))
                    printf '%s' "$n" >"$ORIENT_TEST_COUNTER"
                    ts=$(printf '2026-01-01T%02d:%02d:%02dZ' $((n/3600)) $(((n/60)%60)) $((n%60)))
                    jq --arg body "$body" --arg ts "$ts" \\
                      '. + [{body: $body, createdAt: $ts, author: {login: "shared-account"}}]' \\
                      "$ORIENT_TEST_COMMENTS" >"$ORIENT_TEST_COMMENTS.tmp"
                    mv "$ORIENT_TEST_COMMENTS.tmp" "$ORIENT_TEST_COMMENTS"
                    ;;
                  "pr list"|"api")
                    printf '[]\\n'
                    ;;
                  *)
                    printf '[]\\n'
                    ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    def run_orient(self, agents: str) -> subprocess.CompletedProcess[str]:
        agents_file = self.base / "agents.json"
        agents_file.write_text(agents, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            ORIENT_TEST_AGENTS=bash_path(agents_file),
            ORIENT_TEST_COMMENTS=bash_path(self.comments),
            ORIENT_TEST_COUNTER=bash_path(self.counter),
            AI_TEAM_TEST_ORIGIN_REPO="example/canonical",
            PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
        )
        return run_with_bash_path(
            ["bash", bash_path(self.scripts / "orient.sh")],
            stub_directory=self.bin,
            cwd=self.base,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def seed_proposal(self, decision_id="locale-order"):
        self.comments.write_text(
            '[{"body": "**From:** p\\n\\n**Type:** proposal\\n\\n'
            f'**Decision:** {decision_id}\\n**Options:** keep,swap\\n**Recommend:** keep\\n'
            '**Deadline:** 2099-01-01T00:00:00Z\\n\\nWhich way?\\n", '
            '"createdAt": "2025-01-01T00:00:01Z"}]',
            encoding="utf-8",
        )

    def test_no_open_proposals_reads_as_none_not_owner_waiting(self):
        result = self.run_orient('["p"]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("== open deliberations", result.stdout)
        self.assertIn("no open proposals", result.stdout)
        self.assertNotIn("waiting for owner", result.stdout.lower())

    def test_an_open_proposal_is_surfaced_with_its_deadline_state(self):
        self.seed_proposal("locale-order")
        result = self.run_orient('["p"]')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("locale-order", result.stdout)
        self.assertIn("open", result.stdout)
        self.assertNotIn("waiting for owner", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
