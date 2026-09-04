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
# Repository settings are only the first layer: target-branch protection and
# every active matching ruleset may narrow the effective set further (#95).
# `LAND_MERGE_METHOD=merge|squash|rebase` selects from that effective set; it
# never bypasses policy, and — unlike the remedy #68 describes — is honoured
# on the path that prints it.
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
  --json number,title,body,headRefName,baseRefName,labels,isDraft,state,mergeCommit,comments 2>/dev/null) || {
  echo "cannot read PR #$pr from $repo" >&2
  exit 2
}
pr_identity=$(gh api "repos/$repo/pulls/$pr" 2>/dev/null) || {
  echo "cannot read immutable PR identity for #$pr from $repo" >&2
  exit 2
}

title=$(jq -r '.title // ""' <<<"$pr_json")
branch=$(jq -r '.headRefName // ""' <<<"$pr_json")
base_branch=$(jq -r '.baseRefName // ""' <<<"$pr_json")
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

  # Resolve the effective merge methods from every GitHub policy layer. The
  # branch-rules endpoint already returns every active repository and parent
  # ruleset matching this exact base branch; intersecting every pull_request
  # rule therefore handles overlapping rulesets without reimplementing GitHub's
  # ref-pattern grammar. Classic protection is a separate, layered API.
  merge_method="${LAND_MERGE_METHOD:-}"
  case "$merge_method" in
    ""|merge|squash|rebase) ;;
    *)
      echo "LAND_MERGE_METHOD must be merge, squash, or rebase (got '$merge_method')" >&2
      exit 2
      ;;
  esac
  [[ -n "$base_branch" ]] || {
    echo "cannot determine PR #$pr base branch; refusing to guess merge policy" >&2
    exit 2
  }

  if ! merge_settings=$(gh api "repos/$repo" 2>&1); then
    echo "cannot read repository merge settings for $repo; refusing to guess merge policy" >&2
    printf '%s\n' "$merge_settings" >&2
    exit 2
  fi

  encoded_base=$(jq -rn --arg value "$base_branch" '$value | @uri')
  if ! applied_rules=$(gh api --paginate --slurp \
    "repos/$repo/rules/branches/$encoded_base?per_page=100" 2>&1); then
    echo "cannot read active rulesets for $repo branch '$base_branch'; refusing to guess merge policy" >&2
    printf '%s\n' "$applied_rules" >&2
    exit 2
  fi

  classic_linear=false
  if ! branch_protection=$(gh api "repos/$repo/branches/$encoded_base/protection" 2>&1); then
    # GitHub uses the canonical structured 404 below for a genuinely
    # unprotected branch. gh currently concatenates its diagnostic directly
    # after the JSON body because that body has no newline. Accept that exact
    # production shape or the exact JSON body alone. A generic/concealed 404,
    # extra JSON, malformed suffix, or contradictory diagnostic is not proof
    # that no policy exists and therefore fails closed.
    if ! AI_TEAM_PROTECTION_RESPONSE="$branch_protection" python3 - <<'PY'
import json
import os
import sys


response = os.environ["AI_TEAM_PROTECTION_RESPONSE"]
diagnostic = "gh: Branch not protected (HTTP 404)"
body = response
if response.endswith(diagnostic):
    body = response[:-len(diagnostic)]
    # The production shape is a direct concatenation. Whitespace before the
    # diagnostic is a different, unsupported response and remains ambiguous.
    if not body or body[-1].isspace():
        sys.exit(1)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


try:
    parsed = json.loads(body, object_pairs_hook=unique_object)
except (json.JSONDecodeError, ValueError):
    sys.exit(1)

expected = {
    "message": "Branch not protected",
    "documentation_url": (
        "https://docs.github.com/rest/branches/"
        "branch-protection#get-branch-protection"
    ),
    "status": "404",
}
sys.exit(0 if parsed == expected else 1)
PY
    then
      echo "cannot read classic protection for $repo branch '$base_branch'; refusing to guess merge policy" >&2
      printf '%s\n' "$branch_protection" >&2
      exit 2
    fi
  else
    # Do not use jq -e here: a valid JSON false is an exit-1 result under -e
    # and would be indistinguishable from malformed protection (#95).
    if ! classic_linear=$(jq -r '
      if has("required_linear_history") then
        if (.required_linear_history | type) == "object"
            and (.required_linear_history | has("enabled"))
            and (.required_linear_history.enabled | type) == "boolean" then
          .required_linear_history.enabled
        else error("invalid required_linear_history") end
      else false end
    ' \
      <<<"$branch_protection" 2>/dev/null); then
      echo "classic protection for $repo branch '$base_branch' has an ambiguous required_linear_history value" >&2
      exit 2
    fi
  fi

  if ! effective_methods=$(jq -enr \
    --argjson settings "$merge_settings" \
    --argjson pages "$applied_rules" \
    --argjson classic_linear "$classic_linear" '
      def known_method: . == "merge" or . == "squash" or . == "rebase";
      def intersect($left; $right): [$left[] | select(. as $method | $right | index($method))];
      if ($settings.allow_merge_commit | type) != "boolean"
          or ($settings.allow_squash_merge | type) != "boolean"
          or ($settings.allow_rebase_merge | type) != "boolean" then
        error("repository merge settings are not booleans")
      elif ($pages | type) != "array" or any($pages[]; type != "array") then
        error("active-rules response is not paginated JSON")
      else
        [if $settings.allow_merge_commit then "merge" else empty end,
         if $settings.allow_squash_merge then "squash" else empty end,
         if $settings.allow_rebase_merge then "rebase" else empty end] as $repository_methods
        | ($pages | add) as $rules
        | if any($rules[]; type != "object" or (.type | type) != "string") then
            error("active-rules response contains an invalid rule")
          else
            reduce $rules[] as $rule (
              if $classic_linear then ($repository_methods - ["merge"]) else $repository_methods end;
              if $rule.type == "required_linear_history" then
                . - ["merge"]
              elif $rule.type == "pull_request" then
                if ($rule.parameters.allowed_merge_methods | type) != "array"
                    or ($rule.parameters.allowed_merge_methods | length) == 0
                    or any($rule.parameters.allowed_merge_methods[]; (type != "string") or (known_method | not)) then
                  error("pull_request rule has invalid allowed_merge_methods")
                else
                  intersect(.; $rule.parameters.allowed_merge_methods)
                end
              elif $rule.type == "merge_queue" then
                error("an active merge_queue rule requires the merge queue")
              else . end
            ) | join(" ")
          end
      end
    ' 2>&1); then
    echo "cannot resolve merge policy for $repo branch '$base_branch': $effective_methods" >&2
    exit 2
  fi

  if [[ -z "$effective_methods" ]]; then
    echo "refusing #$pr: repository settings and target-branch rules allow no common merge method" >&2
    exit 1
  fi
  if [[ -n "$merge_method" ]]; then
    if [[ " $effective_methods " != *" $merge_method "* ]]; then
      echo "refusing #$pr: LAND_MERGE_METHOD=$merge_method is forbidden by effective policy for '$base_branch' (allowed: $effective_methods)" >&2
      exit 1
    fi
  elif [[ " $effective_methods " == *" merge "* ]]; then
    merge_method=merge
  elif [[ " $effective_methods " == *" squash "* ]]; then
    merge_method=squash
  else
    merge_method=rebase
  fi
  echo "landing #$pr with merge_method=$merge_method (effective methods for '$base_branch': $effective_methods)" >&2

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
