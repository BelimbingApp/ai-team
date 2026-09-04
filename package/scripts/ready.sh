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
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$here/_default_branch.sh"

if [[ $# -ne 1 || ! "$pr" =~ ^[0-9]+$ ]]; then
  echo "usage: CLAIM_AGENT=<stable-agent-id> $0 <pr-number>" >&2
  exit 2
fi

if [[ ! "$agent" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
  echo "CLAIM_AGENT must be a lower-case stable agent id (without agent:)" >&2
  exit 2
fi

repo=$(ai_team_origin_repo) || {
  echo "cannot resolve the repository from origin" >&2
  exit 2
}
[[ -n "$repo" ]] || { echo "cannot resolve the repository from origin" >&2; exit 2; }

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

# #80: Copilot's findings live on the inline-comment / review-thread surface,
# which the Independent review gate deliberately does not read (no corroborable
# commit_id, no **From:** grammar). Authors must triage those threads before
# dispatch — resolve each one after fixing or declining with a reason — or the
# paid review is discarded by construction. This check does not widen the gate.
owner="${repo%%/*}"
name="${repo#*/}"
threads_json=$(gh api graphql \
  -f "query=query(\$o:String!,\$n:String!,\$p:Int!){repository(owner:\$o,name:\$n){pullRequest(number:\$p){reviewThreads(first:100){nodes{isResolved comments(first:10){nodes{author{login} body url}}}}}}}" \
  -f "o=$owner" -f "n=$name" -F "p=$pr" 2>/dev/null) || threads_json=""
if [[ -z "$threads_json" ]]; then
  echo "warning #$pr: cannot read review threads to confirm Copilot triage (#80); proceeding without thread evidence" >&2
  unresolved=""
else
  unresolved=$(jq -r '
    if (.data.repository.pullRequest.reviewThreads.nodes | type) != "array" then
      error("reviewThreads.nodes missing")
    else
      [.data.repository.pullRequest.reviewThreads.nodes[]
       | select(.isResolved | not)
       | select([.comments.nodes[]?.author.login // ""]
                | any(test("copilot"; "i")))
       | (.comments.nodes[0].url // "thread") as $url
       | (.comments.nodes[0].body // "") as $body
       | "  - \($url)\n    \($body | split("\n")[0] | .[0:120])"
      ] | join("\n")
    end
  ' <<<"$threads_json") || {
    echo "warning #$pr: cannot parse review threads for Copilot triage (#80); proceeding without thread evidence" >&2
    unresolved=""
  }
fi
if [[ -n "$unresolved" ]]; then
  echo "refusing #$pr: unresolved Copilot review thread(s) — triage before ready.sh (#80):" >&2
  echo "$unresolved" >&2
  echo "Resolve each thread after fixing or declining with a reason, then re-run." >&2
  exit 1
fi

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
