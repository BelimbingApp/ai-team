#!/usr/bin/env bash
#
# Hand a claimed draft PR to independent review. Re-asserts the issue-closing
# keyword before the author (or anyone) can leave draft with a rewritten body
# that dropped what claim.sh wrote.
#
#   CLAIM_AGENT=<stable-agent-id> docs/ai-team/scripts/ready.sh <pr-number>
#
# Optional: READY_ISSUE=<n> when the PR title/branch do not carry a trailing
# (#n) / issue-<n>, or to confirm an identity that must agree with them.
# The agent label on the PR must match CLAIM_AGENT.

set -euo pipefail

pr="${1:-}"
agent="${CLAIM_AGENT:-}"
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=docs/ai-team/scripts/_lane_issue.sh
source "$here/_lane_issue.sh"

if [[ $# -ne 1 || ! "$pr" =~ ^[0-9]+$ ]]; then
  echo "usage: CLAIM_AGENT=<stable-agent-id> $0 <pr-number>" >&2
  exit 2
fi

if [[ ! "$agent" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
  echo "CLAIM_AGENT must be a lower-case stable agent id (without agent:)" >&2
  exit 2
fi

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) || {
  echo "cannot resolve the repository from gh" >&2
  exit 2
}

pr_json=$(gh pr view "$pr" --repo "$repo" \
  --json number,title,body,headRefName,labels,isDraft,state 2>/dev/null) || {
  echo "cannot read PR #$pr from $repo" >&2
  exit 2
}

state=$(jq -r .state <<<"$pr_json")
if [[ "$state" != "OPEN" ]]; then
  echo "refusing #$pr: state is $state" >&2
  exit 1
fi

holders=$(jq -r '[.labels[].name | select(startswith("agent:"))] | join(",")' <<<"$pr_json")
if [[ "$holders" != "agent:$agent" ]]; then
  echo "refusing #$pr: expected sole owner agent:$agent, found [${holders:-none}]" >&2
  exit 1
fi

title=$(jq -r '.title // ""' <<<"$pr_json")
branch=$(jq -r '.headRefName // ""' <<<"$pr_json")
body=$(jq -r '.body // ""' <<<"$pr_json")
derived=$(ai_team_derive_lane_issue "$title" "$branch" "$body" "${READY_ISSUE:-}")

if [[ "$derived" == error:* ]]; then
  echo "refusing #$pr: ${derived#error:}" >&2
  exit 1
fi

if [[ "$derived" == "none" ]]; then
  echo "refusing #$pr: AI-Team-Lane-Issue: none is a gate path, not a ready.sh handoff" >&2
  exit 1
fi

issue="$derived"

# Keep Closes for consistency with claim.sh when the shared contract reports
# that no GitHub closing keyword is bound to this lane issue.
if ! ai_team_body_has_closing_reference "$body" "$issue"; then
  if [[ -n "$body" && "$body" != *$'\n' ]]; then
    body+=$'\n'
  fi
  body+=$'\n'"Closes #${issue}"$'\n'
  body_file=$(mktemp)
  trap 'rm -f "$body_file"' EXIT
  printf '%s' "$body" >"$body_file"
  gh pr edit "$pr" --repo "$repo" --body-file "$body_file"
  echo "re-asserted Closes #$issue on PR #$pr"
fi

if [[ $(jq -r .isDraft <<<"$pr_json") == "true" ]]; then
  gh pr ready "$pr" --repo "$repo"
fi

# Label edits are best-effort when a label is already absent/present.
gh pr edit "$pr" --repo "$repo" --remove-label task:active >/dev/null 2>&1 || true
gh pr edit "$pr" --repo "$repo" --add-label task:review >/dev/null
gh issue edit "$issue" --repo "$repo" --remove-label task:active >/dev/null 2>&1 || true
gh issue edit "$issue" --repo "$repo" --add-label task:review >/dev/null

echo "PR #$pr ready for review (Closes #$issue)"
