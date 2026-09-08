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
# REST has a separate rate-limit budget from the GraphQL queries used by the
# rest of orientation. This probe must remain available when that shared
# GraphQL budget is exhausted. The Issues endpoint also returns pull requests,
# so explicitly exclude them before treating a label as a team-wide halt.
if ! halt=$(gh api "repos/$repo/issues?state=open&labels=ops:halt&per_page=100" \
  --jq '.[]|select(.pull_request == null)|"  HALT #\(.number) — \(.title)"' 2>/dev/null); then
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
