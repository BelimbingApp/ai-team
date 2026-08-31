#!/usr/bin/env bash
# Report task-label drift on open and recently closed issues.
#
#   docs/ai-team/scripts/label_hygiene.sh <owner/repository>
#
# The recent closed window catches lanes that reached GitHub's CLOSED state
# without passing through land.sh's task:done transition. Historical labels are
# left intact unless a task explicitly repairs them.

set -uo pipefail

repo="${1:-}"
if [ -z "$repo" ]; then
  echo "usage: label_hygiene.sh <owner/repository>" >&2
  exit 2
fi

echo "== label hygiene — these are invisible to the queries above =="
gh issue list --repo "$repo" --state open --label "task:done" --limit 40 \
  --json number,title --jq '.[]|"  #\(.number) OPEN but labelled task:done — \(.title[0:56])"' 2>/dev/null
gh issue list --repo "$repo" --state open --limit 100 --json number,title,labels \
  --jq '[.[]|select([.labels[].name]|map(select(startswith("task:")))|length > 1)]
        |.[]|"  #\(.number) carries two task:* labels — \(.title[0:56])"' 2>/dev/null

closed_days="${AI_TEAM_CLOSED_HYGIENE_DAYS:-30}"
if [[ ! "$closed_days" =~ ^[0-9]+$ ]]; then
  closed_days=30
fi
closed_cutoff=$(date -u -d "-$closed_days days" +%Y-%m-%d 2>/dev/null \
  || date -u -v-"${closed_days}"d +%Y-%m-%d 2>/dev/null \
  || date -u +%Y-%m-%d)

if ! closed_lanes=$(gh issue list --repo "$repo" --state closed \
  --search "closed:>=$closed_cutoff" --limit 100 \
  --json number,title,labels,closedAt 2>/dev/null); then
  echo "  unavailable — could not inspect recently closed lanes"
  exit 0
fi

printf '%s\n' "$closed_lanes" | jq -r '
  .[]
  | ([.labels[]?.name | select(startswith("task:"))]) as $tasks
  | select(any($tasks[]?; . != "task:done"))
  | if (($tasks | length) > 1)
    then "  #\(.number) CLOSED has contradictory task labels [\($tasks | join(", "))] — \(.title[0:56])"
    else "  #\(.number) CLOSED has non-terminal task label(s) [\($tasks | join(", "))] — \(.title[0:56])"
    end
' 2>/dev/null
