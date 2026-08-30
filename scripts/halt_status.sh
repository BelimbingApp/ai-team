#!/usr/bin/env bash
# Print the shared board halt state. Unknown state fails closed: an agent that
# cannot prove work is allowed must not claim something new.

set -u

repo="${1:-}"
if [ -z "$repo" ]; then
  echo "usage: halt_status.sh <owner/repository>" >&2
  exit 2
fi

echo "== operations =="
if ! halt=$(gh issue list --repo "$repo" --state open --label "ops:halt" \
  --json number,title --jq '.[]|"  HALT #\(.number) — \(.title)"' 2>/dev/null); then
  echo "  *** HALT STATUS UNKNOWN — STAND DOWN ***"
  echo "  Cannot query the shared board; do not claim new work."
  exit 2
fi

if [ -n "$halt" ]; then
  echo "  *** STAND DOWN — a halt is active ***"
  printf '%s\n' "$halt"
  echo "  Finish or hand off your current PR, run docs/ai-team/scripts/cleanup.sh,"
  echo "  cancel your heartbeat and any watcher, and go silent. Stop is not idle."
  exit 3
fi

echo "  ok      no halt active"
