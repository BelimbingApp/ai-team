#!/usr/bin/env bash
#
# Gate, merge, and terminalize a lane in one recoverable command.
#
#   LAND_AGENT=<stable-agent-id> docs/ai-team/scripts/land.sh <pr> <reviewed-sha>
#
# The gate is always the merge precondition. Once the PR is merged, the script
# moves both the PR and its lane issue to task:done and records the acting agent.
# A rerun against an already-merged PR only retries terminalization, which makes
# a transient label or comment failure recoverable without attempting a second
# merge.
#
set -euo pipefail

pr="${1:-}"
reviewed="${2:-}"
agent="${LAND_AGENT:-${CLAIM_AGENT:-}}"
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# shellcheck source=docs/ai-team/scripts/_lane_issue.sh
# shellcheck disable=SC1091
source "$here/_lane_issue.sh"

if [[ $# -ne 2 || ! "$pr" =~ ^[0-9]+$ || ! "$reviewed" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "usage: LAND_AGENT=<stable-agent-id> $0 <pr-number> <reviewed-full-sha>" >&2
  exit 2
fi

if [[ ! "$agent" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
  echo "LAND_AGENT must be a lower-case stable agent id (without agent:)" >&2
  exit 2
fi

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) || {
  echo "cannot resolve the repository from gh" >&2
  exit 2
}

pr_json=$(gh pr view "$pr" --repo "$repo" \
  --json number,title,body,headRefName,labels,isDraft,state,mergeCommit,comments 2>/dev/null) || {
  echo "cannot read PR #$pr from $repo" >&2
  exit 2
}

title=$(jq -r '.title // ""' <<<"$pr_json")
branch=$(jq -r '.headRefName // ""' <<<"$pr_json")
body=$(jq -r '.body // ""' <<<"$pr_json")
lane_issue=$(ai_team_derive_lane_issue "$title" "$branch" "$body" "")
if [[ "$lane_issue" == error:* ]]; then
  echo "refusing #$pr: ${lane_issue#error:}" >&2
  exit 1
fi

state=$(jq -r '.state // ""' <<<"$pr_json")
merge_sha=""
if [[ "$state" == "OPEN" ]]; then
  if ! "$here/gate.sh" "$pr" "$reviewed"; then
    echo "refusing to merge #$pr: gate failed" >&2
    exit 1
  fi

  merge_json=$(gh api -X PUT "repos/$repo/pulls/$pr/merge" -f merge_method=merge 2>/dev/null) || {
    echo "merge request failed for PR #$pr" >&2
    exit 1
  }
  if [[ "$(jq -r '.merged // false' <<<"$merge_json")" != "true" ]]; then
    message=$(jq -r '.message // "GitHub did not merge the PR"' <<<"$merge_json")
    echo "PR #$pr was not merged: $message" >&2
    exit 1
  fi
  merge_sha=$(jq -r '.sha // empty' <<<"$merge_json")
elif [[ "$state" == "MERGED" ]]; then
  merge_sha=$(jq -r '.mergeCommit.oid // empty' <<<"$pr_json")
  echo "PR #$pr is already merged; retrying terminalization"
else
  echo "refusing #$pr: state is ${state:-unknown}" >&2
  exit 1
fi

if [[ ! "$merge_sha" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "cannot determine the merge SHA for PR #$pr; terminalization stopped" >&2
  exit 1
fi

terminalize() {
  local kind="$1"
  local number="$2"

  gh "$kind" edit "$number" --repo "$repo" \
    --remove-label task:ready \
    --remove-label task:active \
    --remove-label task:review \
    --remove-label task:blocked \
    --add-label task:done >/dev/null
}

terminalize pr "$pr" || {
  echo "PR #$pr merged, but its terminal label transition failed; rerun land.sh to retry" >&2
  exit 1
}

if [[ "$lane_issue" != "none" ]]; then
  terminalize issue "$lane_issue" || {
    echo "PR #$pr merged, but issue #$lane_issue terminalization failed; rerun land.sh to retry" >&2
    exit 1
  }
fi

attribution="**From:** $agent — merged at $merge_sha"
already_attributed=$(jq -r --arg body "$attribution" \
  '[.comments[]?.body // empty | select(. == $body)] | length' <<<"$pr_json")
if [[ "$already_attributed" == "0" ]]; then
  gh pr comment "$pr" --repo "$repo" --body "$attribution" >/dev/null || {
    echo "PR #$pr merged and labels terminalized, but attribution failed; rerun land.sh to retry" >&2
    exit 1
  }
fi

if [[ "$lane_issue" == "none" ]]; then
  echo "PR #$pr merged at $merge_sha (issue-less lane; task:done applied to PR)"
else
  echo "PR #$pr merged at $merge_sha; PR and issue #$lane_issue are task:done"
fi
