#!/usr/bin/env bash
#
# Decide whether a pull request has an independent, exact-head acceptance.
#
# This is the package's single review grammar. `gate.sh` uses it as its
# pre-flight review step; the GitHub Actions workflow uses it as the required
# CI check. Keeping those two callers behind this file prevents a valid review
# in one place from being invisible in the other.
#
#   package/scripts/review_gate.sh <pr-number> [<reviewed-full-sha>]
#   REVIEW_GATE_REPOSITORY=<owner/repo> package/scripts/review_gate.sh <pr> [<sha>]
#   REVIEW_GATE_INPUT=<fixture.json> package/scripts/review_gate.sh
#
# In an adopter those paths start with docs/ai-team/scripts/. A standalone copy
# downloaded by the trusted workflow has no sibling helper or Git checkout, so
# live workflow callers pass REVIEW_GATE_REPOSITORY explicitly. A packaged or
# mounted copy has the helper and always treats origin as authoritative; an
# inherited override cannot split review reads from gate.sh's repository.
#
# Fixture input has `reviewed`, `labels`, and `reviews` fields. `labels` may be
# an array of label names or GitHub label objects; `reviews` uses the API shape.
# Exit 0 means an independent acceptance exists and no independent
# changes-required verdict supersedes it. Exit 1 is a review failure; exit 2 is
# an invocation or GitHub-read failure.

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
input="${REVIEW_GATE_INPUT:-}"
cleanup_paths=()

cleanup() {
  local path
  for path in "${cleanup_paths[@]}"; do
    [[ -n "$path" ]] && rm -f -- "$path"
  done
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -z "$input" ]]; then
  pr="${1:-}"
  reviewed="${2:-}"
  if [[ ! "$pr" =~ ^[0-9]+$ ]]; then
    echo "ERROR: usage: review_gate.sh <pr-number> [<reviewed-full-sha>]" >&2
    exit 2
  fi

  helper="$here/_default_branch.sh"
  if [[ -r "$helper" ]]; then
    # Fixture mode never reaches this source. A local package or mounted gate
    # must share gate.sh's origin repository even if the caller inherited a
    # REVIEW_GATE_REPOSITORY intended for some other command.
    # shellcheck source=docs/ai-team/scripts/_default_branch.sh
    # shellcheck disable=SC1091
    source "$helper"
    repo=$(ai_team_origin_repo) || {
      echo "ERROR: cannot resolve this repository from origin" >&2
      exit 2
    }
  else
    # The trusted workflow downloads only this script, not its sibling helper.
    # That standalone shape has no origin and must receive the repository from
    # the trusted workflow context.
    repo="${REVIEW_GATE_REPOSITORY:-}"
    if [[ -z "$repo" ]]; then
      echo "ERROR: REVIEW_GATE_REPOSITORY is required when the origin helper is unavailable" >&2
      exit 2
    fi
  fi
  if [[ ! "$repo" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "ERROR: REVIEW_GATE_REPOSITORY must be an owner/repository name" >&2
    exit 2
  fi
  pr_json_file=$(mktemp) || {
    echo "ERROR: cannot create temporary review input" >&2
    exit 2
  }
  cleanup_paths+=("$pr_json_file")
  if ! gh pr view "$pr" --repo "$repo" --json headRefOid,labels >"$pr_json_file" 2>/dev/null; then
    echo "ERROR: cannot read PR #$pr from $repo" >&2
    exit 2
  fi
  if [[ -z "$reviewed" ]]; then
    reviewed=$(jq -r '.headRefOid // ""' "$pr_json_file")
  fi
  if [[ ! "$reviewed" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: reviewed SHA must be a full 40-character lowercase SHA" >&2
    exit 2
  fi

  reviews_file=$(mktemp) || {
    echo "ERROR: cannot create temporary review input" >&2
    exit 2
  }
  cleanup_paths+=("$reviews_file")
  if ! gh api "repos/$repo/pulls/$pr/reviews" --paginate 2>/dev/null \
    | jq -s 'add // []' >"$reviews_file" 2>/dev/null; then
    echo "ERROR: cannot read reviews for PR #$pr from $repo" >&2
    exit 2
  fi

  input=$(mktemp) || {
    echo "ERROR: cannot create temporary review input" >&2
    exit 2
  }
  cleanup_paths+=("$input")
  jq -n --arg reviewed "$reviewed" \
    --slurpfile pr "$pr_json_file" \
    --slurpfile reviews "$reviews_file" \
    '{reviewed: $reviewed, labels: ($pr[0].labels // []), reviews: ($reviews[0] // [])}' >"$input"
fi

result=$(jq -r '
  def label_names:
    if (.labels | type) != "array" then []
    elif (.labels | length) == 0 then []
    elif (.labels[0] | type) == "string" then .labels
    else [.labels[].name // empty]
    end;
  def from_agent:
    ([((.body // "") | split("\n")[]
       | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<agent>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").agent
       | ascii_downcase)] | unique) as $agents
    | if ($agents | length) == 1 then $agents[0] else "" end;
  def explicit_verdicts:
    [((.body // "") | split("\n")[]
       | capture("^\\*\\*Verdict:\\*\\*[[:space:]]*(?<verdict>accept(?: with follow-up)?|changes required)[[:space:]]*$"; "i").verdict
       | ascii_downcase)] | unique;
  def review_verdict:
    explicit_verdicts as $explicit
    | if .state == "DISMISSED" then ""
      elif .state == "CHANGES_REQUESTED" then "changes required"
      elif ($explicit | length) > 1 then ""
      elif ($explicit | length) == 1 and $explicit[0] == "changes required" then "changes required"
      elif .state == "APPROVED"
           or (($explicit | length) == 1 and ($explicit[0] == "accept" or $explicit[0] == "accept with follow-up"))
      then "accept"
      else ""
      end;
  . as $input
  | (label_names) as $labels
  | ([$labels[] | select(startswith("agent:")) | ltrimstr("agent:")] | unique) as $authors
  | if ($authors | length) != 1 then
      ["FAIL: expected exactly one agent:<id> author lane, found \($authors | length)"]
    else
      ($authors[0]) as $author
      | [$input.reviews[]
         | select(.commit_id == $input.reviewed)
         | . + {agent: from_agent, verdict: review_verdict}
         | select(.agent != "")]
        | sort_by(.agent, .submitted_at, .id)
        | group_by(.agent)
        | map(last) as $latest
      | ([$latest[] | select(.agent != $author and .verdict == "accept") | .agent] | unique | join(", ")) as $accepted
      | ([$latest[] | select(.agent != $author and .verdict == "changes required") | .agent] | unique | join(", ")) as $blocking
      | ([$latest[] | select(.agent != $author and .verdict == "") | .agent] | unique) as $malformed
      | [if $accepted == "" then
           "FAIL: no independent exact-head acceptance; require **From:** <reviewer> plus APPROVED or **Verdict:** accept"
         else
           "PASS: independent exact-head acceptance from \($accepted)"
         end,
         if $blocking == "" then
           "PASS: no independent exact-head changes-required verdict"
         else
           "FAIL: independent exact-head changes required by \($blocking)"
         end]
        + [$malformed[] | "WARN: a review marker from \(.) was seen at \($input.reviewed[0:8]) but rejected for format — **Verdict:** must stand alone on its own line (accept / accept with follow-up / changes required)"]
    end
  | .[]
' "$input" 2>/dev/null) || {
  echo "ERROR: review input is malformed" >&2
  exit 2
}

printf '%s\n' "$result"
if grep -q '^FAIL:' <<<"$result"; then
  exit 1
fi
