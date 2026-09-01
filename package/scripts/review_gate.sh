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
# An APPROVED review naming no agent is dropped before any verdict is
# computed, so it is named in a WARN: an approval that does not count must
# never be silent, or its author cannot learn why the gate ignored them.
#
# Fixture input has `reviewed`, `head_sha`, `labels`, `identity`, and `reviews`
# fields. `labels` may be an array of label names or GitHub label objects;
# `identity` is the REST pull-request shape and `reviews` uses the GitHub API
# shape. Every attributable review must name the reviewed full SHA in a
# `**HEAD reviewed:**` marker. GitHub's review `commit_id` remains required as
# corroboration, but is insufficient by itself because a Dependabot rebase can
# rewrite that field on an older review to the replacement head.
# Exit 0 means an independent acceptance exists and no independent
# changes-required verdict supersedes it. Exit 1 is a review failure; exit 2 is
# an invocation or GitHub-read failure.

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# The trusted-author boundary is shared with gate.sh and land.sh. The trusted
# workflow fetches this helper alongside review_gate.sh from the same pinned
# commit, so a standalone copy still finds it next to itself.
# Path is the package layout.
# shellcheck source=package/scripts/_trusted_author.sh
# shellcheck disable=SC1091
source "$here/_trusted_author.sh"
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
  # The immutable REST pull payload supplies head state, labels, and the
  # author identity the trusted-author boundary classifies. REST pull payloads
  # routinely exceed Windows/MSYS process argument limits, so GitHub JSON stays
  # file-backed all the way into jq; never reintroduce it via --argjson or
  # command arguments.
  # All temporaries are allocated before the first network read, so a failed
  # later allocation exits with every earlier one already registered for the
  # EXIT-trap cleanup — nothing is left behind on the partial path.
  input=$(mktemp) || {
    echo "ERROR: cannot allocate temporary review input" >&2
    exit 2
  }
  cleanup_paths+=("$input")
  identity_file=$(mktemp) || {
    echo "ERROR: cannot allocate temporary PR identity input" >&2
    exit 2
  }
  cleanup_paths+=("$identity_file")
  reviews_file=$(mktemp) || {
    echo "ERROR: cannot allocate temporary reviews input" >&2
    exit 2
  }
  cleanup_paths+=("$reviews_file")

  if ! gh api "repos/$repo/pulls/$pr" >"$identity_file" 2>/dev/null; then
    echo "ERROR: cannot read immutable PR identity for #$pr from $repo" >&2
    exit 2
  fi
  head_sha=$(jq -r '.head.sha // "" | ascii_downcase' "$identity_file")
  if [[ ! "$head_sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: current PR head is missing or malformed for #$pr from $repo" >&2
    exit 2
  fi
  if [[ -z "$reviewed" ]]; then
    reviewed="$head_sha"
  fi
  if [[ ! "$reviewed" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: reviewed SHA must be a full 40-character lowercase SHA" >&2
    exit 2
  fi

  if ! gh api "repos/$repo/pulls/$pr/reviews" --paginate 2>/dev/null \
    | jq -s 'add // []' >"$reviews_file" 2>/dev/null; then
    echo "ERROR: cannot read reviews for PR #$pr from $repo" >&2
    exit 2
  fi

  jq -n --arg reviewed "$reviewed" \
    --arg head_sha "$head_sha" \
    --slurpfile identity "$identity_file" \
    --slurpfile reviews "$reviews_file" \
    '($identity[0] // {}) as $pr
     | {reviewed: $reviewed,
        head_sha: $head_sha,
        labels: ($pr.labels // []),
        identity: $pr,
        reviews: ($reviews[0] // [])}' >"$input"
fi

identity_json=$(jq -c '.identity // {}' "$input" 2>/dev/null || printf '{}')
automated_author=$(ai_team_trusted_automated_author_lane "$identity_json")

result=$(jq -r --arg automated_author "$automated_author" '
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
  def reviewed_head:
    ([((.body // "") | split("\n")[]
       | capture("^\\*\\*HEAD reviewed:\\*\\*[[:space:]]*`?(?<sha>[0-9a-f]{40})`?[[:space:]]*$"; "i").sha
       | ascii_downcase)] | unique) as $heads
    | if ($heads | length) == 1 then $heads[0] else "" end;
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
  | if ($input.head_sha // "") != $input.reviewed then
      ["FAIL: reviewed SHA is not the current PR head; re-review the new head"]
    elif $automated_author != "" and ($authors | length) != 0 then
      ["FAIL: trusted automated author \($automated_author) must not carry agent:<id> labels"]
    elif $automated_author == "" and ($authors | length) != 1 then
      ["FAIL: expected exactly one agent:<id> author lane, found \($authors | length)"]
    else
      (if $automated_author != "" then $automated_author else $authors[0] end) as $author
      | [$input.reviews[] | select(.commit_id == $input.reviewed)] as $at_head
      | [$at_head[]
         | . + {agent: from_agent, verdict: review_verdict, reviewed_head: reviewed_head}
         | select(.agent != "")]
        | sort_by(.agent, .submitted_at, .id)
        | group_by(.agent)
        | map(last) as $latest
      | ([$latest[] | select(.agent != $author and .reviewed_head == $input.reviewed and .verdict == "accept") | .agent]
         | unique | join(", ")) as $accepted
      | ([$latest[] | select(.agent != $author and .reviewed_head == $input.reviewed and .verdict == "changes required") | .agent]
         | unique | join(", ")) as $blocking
      | ([$latest[] | select(.agent != $author and .verdict == "") | .agent] | unique) as $malformed
      | ([$latest[] | select(.agent != $author and .reviewed_head != $input.reviewed) | .agent] | unique) as $unbound
      | ([$at_head[]
          | select(.state == "APPROVED")
          | . + {agent: from_agent}
          | select(.agent == "")
          | (.user.login? // "an unidentified account")]
         | unique) as $unattributed
      | [if $accepted == "" then
           "FAIL: no independent exact-head acceptance; require **From:** <reviewer>, **HEAD reviewed:** `<full-sha>`, and APPROVED or **Verdict:** accept"
         else
           "PASS: independent exact-head acceptance from \($accepted)"
         end,
         if $blocking == "" then
           "PASS: no independent exact-head changes-required verdict"
         else
           "FAIL: independent exact-head changes required by \($blocking)"
         end]
        + [$unattributed[] | "WARN: an APPROVED review from \(.) was ignored: it carries no **From:** marker"]
        + [$unbound[] | "WARN: a review marker from \(.) was rejected because **HEAD reviewed:** must name exact head \($input.reviewed)"]
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
