#!/usr/bin/env bash
#
# Gate, merge, and terminalize a lane in one recoverable command.
#
#   LAND_AGENT=<stable-agent-id> docs/ai-team/scripts/land.sh <pr> <reviewed-sha>
#
# The merge method is not the package's to choose. A repository that forbids
# merge commits answers a hardcoded `merge_method=merge` with a 405 *after* a
# full GATE: PASS, which reads as the gate lying (#66). The method is resolved
# from the repository's own `allow_*_merge` settings, preferring `merge` so
# history keeps the reviewed commit intact, then `squash`, then `rebase`.
# `LAND_MERGE_METHOD=merge|squash|rebase` overrides that, and — unlike the
# remedy #68 describes — it is honoured on the path that prints it.
#
# The gate is always the merge precondition. Once the PR is merged, the script
# moves both the PR and its lane issue to task:done and records the acting agent.
# A trusted automated issue-less lane terminalizes only its PR. A rerun against
# an already-merged PR only retries terminalization, which makes a transient
# label or comment failure recoverable without attempting a second merge.
#
set -euo pipefail

pr="${1:-}"
reviewed="${2:-}"
agent="${LAND_AGENT:-${CLAIM_AGENT:-}}"
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# shellcheck source=docs/ai-team/scripts/_lane_issue.sh
# shellcheck disable=SC1091
source "$here/_lane_issue.sh"
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$here/_default_branch.sh"
# Path is the package layout.
# shellcheck source=package/scripts/_trusted_author.sh
# shellcheck disable=SC1091
source "$here/_trusted_author.sh"

if [[ $# -ne 2 || ! "$pr" =~ ^[0-9]+$ || ! "$reviewed" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "usage: LAND_AGENT=<stable-agent-id> $0 <pr-number> <reviewed-full-sha>" >&2
  exit 2
fi
reviewed="${reviewed,,}"

if [[ ! "$agent" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
  echo "LAND_AGENT must be a lower-case stable agent id (without agent:)" >&2
  exit 2
fi

repo=$(ai_team_origin_repo) || {
  echo "cannot resolve the repository from origin" >&2
  exit 2
}
[[ -n "$repo" ]] || { echo "cannot resolve the repository from origin" >&2; exit 2; }

pr_json=$(gh pr view "$pr" --repo "$repo" \
  --json number,title,body,headRefName,labels,isDraft,state,mergeCommit,comments 2>/dev/null) || {
  echo "cannot read PR #$pr from $repo" >&2
  exit 2
}
pr_identity=$(gh api "repos/$repo/pulls/$pr" 2>/dev/null) || {
  echo "cannot read immutable PR identity for #$pr from $repo" >&2
  exit 2
}

title=$(jq -r '.title // ""' <<<"$pr_json")
branch=$(jq -r '.headRefName // ""' <<<"$pr_json")
body=$(jq -r '.body // ""' <<<"$pr_json")
automated_author=$(ai_team_trusted_automated_author_lane "$pr_identity")
if [[ -n "$automated_author" ]]; then
  lane_issue="none"
else
  lane_issue=$(ai_team_derive_lane_issue "$title" "$branch" "$body" "${READY_ISSUE:-}")
fi
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

  # Resolve the merge method from the repository rather than assuming one.
  # An explicit override wins; otherwise prefer `merge` (the reviewed commit
  # survives verbatim), then `squash`, then `rebase`. A repository that allows
  # none of the three cannot be landed into by any method, and saying so beats
  # a 405 the operator has to decode.
  merge_method="${LAND_MERGE_METHOD:-}"
  if [[ -n "$merge_method" ]]; then
    case "$merge_method" in
      merge|squash|rebase) ;;
      *)
        echo "LAND_MERGE_METHOD must be merge, squash, or rebase (got '$merge_method')" >&2
        exit 2
        ;;
    esac
  else
    if ! merge_settings=$(gh api "repos/$repo" \
      --jq '[(.allow_merge_commit // false), (.allow_squash_merge // false), (.allow_rebase_merge // false)] | @tsv' 2>&1); then
      echo "cannot read merge settings for $repo; set LAND_MERGE_METHOD=merge|squash|rebase" >&2
      printf '%s\n' "$merge_settings" >&2
      exit 2
    fi
    IFS=$'\t' read -r allow_merge allow_squash allow_rebase <<<"$merge_settings"
    if [[ "$allow_merge" == "true" ]]; then
      merge_method=merge
    elif [[ "$allow_squash" == "true" ]]; then
      merge_method=squash
    elif [[ "$allow_rebase" == "true" ]]; then
      merge_method=rebase
    else
      echo "refusing #$pr: $repo allows no merge method (merge, squash and rebase are all disabled)" >&2
      exit 1
    fi
  fi
  [[ "$merge_method" == "merge" ]] \
    || echo "landing #$pr with merge_method=$merge_method ($repo does not allow a merge commit)" >&2

  # A passed gate establishes the AI Team's own exact-head review evidence; it
  # cannot waive GitHub branch protections. Keep GitHub's response visible on
  # an endpoint failure, then name that boundary so a shared account is not
  # mistaken for a native approving reviewer (#35).
  if ! merge_json=$(gh api -X PUT "repos/$repo/pulls/$pr/merge" \
    -f merge_method="$merge_method" -f sha="$reviewed" 2>&1); then
    echo "merge request failed for PR #$pr" >&2
    if [[ -n "$merge_json" ]]; then
      printf '%s\n' "$merge_json" >&2
    fi
    cat >&2 <<'TXT'
The AI Team gate does not override GitHub branch protections or other repository
merge rules. If this repository requires a native GitHub approval, obtain one
from a separate eligible reviewer or automation; only the repository owner can
intentionally change that external rule.
TXT
    exit 1
  fi
  if [[ "$(jq -r '.merged // false' <<<"$merge_json")" != "true" ]]; then
    message=$(jq -r '.message // "GitHub did not merge the PR"' <<<"$merge_json")
    echo "PR #$pr was not merged: $message" >&2
    cat >&2 <<'TXT'
The AI Team gate does not override GitHub branch protections or other repository
merge rules. If this repository requires a native GitHub approval, obtain one
from a separate eligible reviewer or automation; only the repository owner can
intentionally change that external rule.
TXT
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

# A branch deletion after landing is the documented cleanup, and it silently
# closes any pull request stacked on that branch — GitHub auto-closes a PR whose
# base disappears, with no merge, no comment and no notification, leaving its
# exact-head reviews attached to a dead lane (#69). The person running
# `--delete-branch` usually cannot know a stack exists; land.sh can, because it
# already knows the branch it just merged. Warn by name rather than delete or
# refuse: the deletion is not this script's to make, and a PR is worth more than
# a tidy branch list.
land_base=$(ai_team_default_branch 2>/dev/null || echo "<default-branch>")
stacked=$(gh pr list --repo "$repo" --state open --base "$branch" \
  --json number --jq '[.[].number] | map("#" + tostring) | join(", ")' 2>/dev/null) || stacked=""
if [[ -n "$stacked" ]]; then
  cat >&2 <<TXT
WARNING: $stacked $( [[ "$stacked" == *,* ]] && echo "are" || echo "is" ) stacked on '$branch'.
Do NOT delete that branch yet — GitHub closes a pull request whose base branch
disappears, silently and without merging it. Retarget each one first:
  gh pr edit <number> --repo $repo --base $land_base
TXT
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

if [[ -n "$automated_author" ]]; then
  gh pr edit "$pr" --repo "$repo" --add-label task:done >/dev/null || {
    echo "PR #$pr merged, but its automated terminal label transition failed; rerun land.sh to retry" >&2
    exit 1
  }
else
  terminalize pr "$pr" || {
    echo "PR #$pr merged, but its terminal label transition failed; rerun land.sh to retry" >&2
    exit 1
  }
fi

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
  if [[ -n "$automated_author" ]]; then
    echo "PR #$pr merged at $merge_sha (trusted automated lane; task:done applied to PR)"
  else
    echo "PR #$pr merged at $merge_sha (issue-less lane; task:done applied to PR)"
  fi
else
  echo "PR #$pr merged at $merge_sha; PR and issue #$lane_issue are task:done"
fi
