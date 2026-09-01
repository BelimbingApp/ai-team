#!/usr/bin/env bash
#
# Rerun the trusted Independent review workflow for the current pull-request
# head after a review changes. Review events intentionally do not trigger this
# privileged workflow: rerunning an existing pull_request_target run preserves
# the workflow's trusted revision and avoids executing pull-request-controlled
# workflow code.
#
#   package/scripts/rerun-review-check.sh <pr-number>
#
# In an adopting repository the path is
# `docs/ai-team/scripts/rerun-review-check.sh`.
#
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$here/_default_branch.sh"

pr="${1:-}"
if [[ $# -ne 1 || ! "$pr" =~ ^[0-9]+$ ]]; then
  echo "ERROR: usage: rerun-review-check.sh <pr-number>" >&2
  exit 2
fi

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "ERROR: not a git checkout" >&2
  exit 2
}
cd "$root" || exit 2

repo=$(ai_team_origin_repo) || {
  echo "ERROR: cannot resolve the repository from origin" >&2
  exit 2
}
if [[ ! "$repo" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "ERROR: repository from origin is not an owner/repository name" >&2
  exit 2
fi

read_pr() {
  gh api --method GET \
    -H "Accept: application/vnd.github+json" \
    "repos/$repo/pulls/$pr" 2>/dev/null
}

pr_json=$(read_pr) || {
  echo "ERROR: cannot read PR #$pr from $repo" >&2
  exit 2
}
head_sha=$(jq -r '.head.sha // "" | ascii_downcase' <<<"$pr_json")
head_ref=$(jq -r '.head.ref // ""' <<<"$pr_json")
if [[ ! "$head_sha" =~ ^[0-9a-f]{40}$ || -z "$head_ref" ]]; then
  echo "ERROR: PR #$pr has no valid current head identity" >&2
  exit 2
fi

# The repository-level Actions endpoint supports both filters we need before
# jq sees the response. The raw REST shape includes pull_requests, unlike
# `gh run list --json`, so the selected run can be tied back to this PR too.
runs=$(gh api --method GET --paginate \
  -H "Accept: application/vnd.github+json" \
  "repos/$repo/actions/runs?event=pull_request_target&head_sha=$head_sha&per_page=100" \
  2>/dev/null) || {
  echo "ERROR: cannot read pull_request_target runs for PR #$pr from $repo" >&2
  exit 2
}

run_json=$(printf '%s\n' "$runs" | jq -sc \
  --arg sha "$head_sha" \
  --arg head_ref "$head_ref" \
  --arg pr "$pr" '
  [.[].workflow_runs[]?
   | select(.name == "independent review"
            and .event == "pull_request_target"
            and .head_sha == $sha)
   | select(
       ([.pull_requests[]?.number | tostring] | index($pr)) != null
       or (((.pull_requests // []) | length) == 0 and .head_branch == $head_ref)
     )
   | {id, status, conclusion, created_at, html_url}
  ]
  | sort_by(.created_at, .id)
  | last // empty
') || {
  echo "ERROR: cannot parse workflow runs for PR #$pr" >&2
  exit 2
}

if [[ -z "$run_json" ]]; then
  echo "ERROR: no existing trusted Independent review run matches PR #$pr at head ${head_sha:0:8}" >&2
  echo "Create a fresh pull_request_target run with the next allowed PR event, then retry." >&2
  exit 1
fi

run_id=$(jq -r '.id // empty' <<<"$run_json")
run_status=$(jq -r '.status // empty' <<<"$run_json")
run_url=$(jq -r '.html_url // empty' <<<"$run_json")
if [[ ! "$run_id" =~ ^[0-9]+$ || -z "$run_status" ]]; then
  echo "ERROR: selected Independent review run has malformed identity" >&2
  exit 2
fi

# Close the head-race between the first PR read and the rerun request. A push
# after this read can still race the API call, but it cannot make this helper
# silently claim that the new head was refreshed; the next gate remains bound
# to the exact SHA and will fail until a fresh run exists.
latest_pr_json=$(read_pr) || {
  echo "ERROR: cannot re-read PR #$pr before rerunning its check" >&2
  exit 2
}
latest_head_sha=$(jq -r '.head.sha // "" | ascii_downcase' <<<"$latest_pr_json")
if [[ "$latest_head_sha" != "$head_sha" ]]; then
  echo "ERROR: PR #$pr moved from head ${head_sha:0:8} to ${latest_head_sha:0:8}; refusing to rerun the stale check" >&2
  exit 1
fi

case "$run_status" in
  queued|in_progress|requested|waiting|pending)
    echo "Independent review run $run_id is already $run_status for PR #$pr at head ${head_sha:0:8}; no rerun needed${run_url:+ ($run_url)}"
    exit 0
    ;;
  completed)
    ;;
  *)
    echo "ERROR: selected Independent review run $run_id has unsupported status '$run_status'; refusing to rerun" >&2
    exit 2
    ;;
esac

if ! gh run rerun "$run_id" --repo "$repo"; then
  echo "ERROR: could not rerun Independent review run $run_id for PR #$pr" >&2
  exit 2
fi

echo "reran Independent review run $run_id for PR #$pr at head ${head_sha:0:8}${run_url:+ ($run_url)}"
