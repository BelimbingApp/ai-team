#!/usr/bin/env bash
#
# Gate a pull request merge. Prints every verdict and exits non-zero unless all
# of them pass.
#
#   docs/ai-team/scripts/gate.sh <pr-number> [<reviewed-sha>]
#
# Run it as its OWN command and chain the merge to it:
#
#   REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
#   REVIEWED=<full-reviewed-sha>
#   docs/ai-team/scripts/gate.sh 408 "$REVIEWED" \
#     && gh api -X PUT "repos/$REPO/pulls/408/merge" \
#          -f merge_method=merge -f sha="$REVIEWED"
#
# Never put the checks and the merge inside one compound command where the merge
# can still run when a check fails, and always bind the merge request to the
# reviewed SHA. That is exactly how #382 reached main while BEHIND it: the check
# printed its warning and the merge went ahead anyway.
#
# Why both: Protect Main now requires the six repository/Sonar contexts with
# strict_required_status_checks_policy and no merge bypass actors. This gate is
# still the richer pre-flight: it checks exact reviewed-head identity, lane
# ownership, holds, issue closure, and independent review before a merge call.
#
set -u

PR="${1:-}"
if [[ -z "$PR" ]]; then
  echo "usage: gate.sh <pr-number> [<reviewed-sha>]" >&2
  exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=docs/ai-team/scripts/_lane_issue.sh
# shellcheck disable=SC1091
source "$here/_lane_issue.sh"
# Path is the package layout.
# shellcheck source=package/scripts/_trusted_author.sh
# shellcheck disable=SC1091
source "$here/_trusted_author.sh"
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$here/_default_branch.sh"
BASE=$(ai_team_default_branch)

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git checkout" >&2; exit 2; }
cd "$ROOT" || exit 2

REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)
[[ -n "$REPO" ]] || { echo "cannot resolve the repository from gh" >&2; exit 2; }

# The gate fetches and proves branch containment against *origin*, while gh
# resolves $REPO independently. If origin is a fork, PR lookup can succeed
# against the canonical repository while containment is proven against the
# fork's stale main — a false PASS for a head behind canonical main. So origin
# must BE the canonical repository, verified before any verdict is printed.
# The *resolved* URL is what git will actually fetch from — insteadOf
# rewrites included — and the containment proof is only as canonical as
# that transport. Tests get hermeticity by shimming git on PATH, never by
# weakening this invariant.
origin_url=$(git remote get-url origin 2>/dev/null)
origin_repo=$(printf '%s' "$origin_url" | sed -E 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)##; s#\.git$##')
[[ "$origin_repo" = "$REPO" ]] || {
  echo "origin is '$origin_url' but gh resolves the repository as '$REPO'." >&2
  echo "The gate proves containment against origin/$BASE, so origin must be the" >&2
  echo "canonical repository. Run from a clone whose origin is $REPO." >&2
  exit 2
}

# One fetch of PR state; every check below reads from it.
pr=$(gh pr view "$PR" --repo "$REPO" \
       --json headRefOid,headRefName,title,body,isDraft,state,mergeable,labels,closingIssuesReferences 2>/dev/null)
[[ -n "$pr" ]] || { echo "cannot read PR #$PR from $REPO" >&2; exit 2; }
pr_identity=$(gh api "repos/$REPO/pulls/$PR" 2>/dev/null) || {
  echo "cannot read immutable PR identity for #$PR from $REPO" >&2
  exit 2
}

remote_head=$(printf '%s' "$pr" | jq -r .headRefOid)

REVIEWED="${2:-}"
if [[ -z "$REVIEWED" ]]; then
  REVIEWED="$remote_head"
  echo "note: no reviewed SHA given — gating the current head $REVIEWED."
  echo "      Pass the SHA you actually reviewed, so a push after your review fails this gate."
elif [[ "${#REVIEWED}" -lt 40 ]]; then
  # An abbreviation is only usable once the canonical repository resolves it
  # to exactly one commit; every later comparison and check-run query then
  # uses that full SHA, so the merged-is-verified contract stays exact.
  if [[ "${#REVIEWED}" -lt 12 ]]; then
    echo "reviewed SHA '$REVIEWED' is too short (<12 chars) to identify a commit safely — pass at least 12, ideally all 40." >&2
    exit 2
  fi
  resolved=$(gh api "repos/$REPO/commits/$REVIEWED" --jq .sha 2>/dev/null)
  if [[ "${#resolved}" -ne 40 ]] || printf '%s' "$resolved" | grep -q '[^0-9a-f]'; then
    echo "reviewed SHA '$REVIEWED' does not resolve to a single commit in $REPO (unknown or ambiguous)." >&2
    exit 2
  fi
  echo "note: resolved abbreviated $REVIEWED to $resolved via $REPO."
  REVIEWED="$resolved"
fi

git fetch -q origin "$BASE" 2>/dev/null
if git cat-file -e "${REVIEWED}^{commit}" 2>/dev/null; then
  reviewed_object_available=1
else
  git fetch -q origin "pull/$PR/head" 2>/dev/null
  if git cat-file -e "${REVIEWED}^{commit}" 2>/dev/null; then
    reviewed_object_available=1
  else
    reviewed_object_available=0
  fi
fi

fail=0
say_ok()   { echo "  ok      $*"; }
say_bad()  { echo "  BLOCKED $*"; fail=1; }
say_warn() { echo "  WARN    $*"; }

echo "gate: $REPO #$PR at ${REVIEWED:0:8}"

# 1. Open, and not a draft. A draft is somebody's claim, not a deliverable.
state=$(printf '%s' "$pr" | jq -r .state)
draft=$(printf '%s' "$pr" | jq -r .isDraft)
[[ "$state" = "OPEN" ]] && say_ok "state is OPEN" || say_bad "state is $state"
[[ "$draft" = "false" ]] && say_ok "not a draft" || say_bad "PR is a DRAFT — never merge someone's claim"

# 2. Relationship to main. A branch behind main is only a problem when main
#    moved in a file the branch also changes: then CI green on the branch says
#    nothing about the merged tree (#326 landed red exactly that way), and the
#    verdict must be re-taken on a refreshed head. When the two change disjoint
#    files, the merge is mechanical and requiring a refresh only invalidates a
#    valid exact-head verdict for nothing — that refresh loop was the largest
#    single latency in the acceptance path.
if [[ "$reviewed_object_available" != "1" ]]; then
  say_bad "reviewed SHA $REVIEWED is unavailable after fetching PR #$PR — its history may have been rewritten; re-review the current head"
elif git merge-base --is-ancestor "origin/$BASE" "$REVIEWED" 2>/dev/null; then
  say_ok "contains origin/$BASE ($(git rev-parse --short "origin/$BASE"))"
else
  fork_point=$(git merge-base "origin/$BASE" "$REVIEWED" 2>/dev/null || true)
  overlap=""
  if [[ -n "$fork_point" ]]; then
    overlap=$(comm -12 \
      <(git diff --name-only "$fork_point" "origin/$BASE" | sort -u) \
      <(git diff --name-only "$fork_point" "$REVIEWED" | sort -u))
  fi
  if [[ -z "$fork_point" ]]; then
    say_bad "no merge base with origin/$BASE — merge $BASE into the branch first"
  elif [[ -n "$overlap" ]]; then
    say_bad "BEHIND origin/$BASE ($(git rev-parse --short "origin/$BASE")) and $BASE changed files this branch also changes — merge $BASE in and re-review: $(printf '%s' "$overlap" | tr '\n' ' ')"
  else
    say_warn "behind origin/$BASE ($(git rev-parse --short "origin/$BASE")) in disjoint files — landing without a refresh; CI on $BASE is the proof for the merged tree"
  fi
fi

# 3. Checks on the REVIEWED sha, not on the PR, not on the branch. The heads of
#    the last five merged pull requests supply the expected check names: only
#    names present on every head are universal enough to require. A
#    default-branch tip also carries push- and schedule-only checks, which a
#    pull request can never produce; using that tip as the baseline made those
#    checks permanently missing from ordinary pull requests (#1). Sampling one
#    merged PR had the same failure for path-filtered jobs (#22).
#
#    This is observed repository state, not a count copied into the script; when
#    CI adds or removes a job, the gate follows the recent merged PRs. A passing
#    early check can therefore never authorize a merge while another expected
#    check has not reported on the reviewed SHA.
# Judge the LATEST run of each check NAME, not every run on the SHA. A
# superseded run stays on the commit forever: `concurrency: cancel-in-progress`
# leaves a `cancelled` entry behind whenever a PR is force-pushed or pushed
# twice quickly, and counting it blocked #432 while all four of those checks had
# already passed on the same SHA (#433). `neutral` is likewise not a failure --
# CodeQL reports it transiently before settling.
runs=$(gh api "repos/$REPO/commits/$REVIEWED/check-runs" --paginate 2>/dev/null \
  | jq -sc '[.[].check_runs[]]')

# The first pull request may have no historical PR head. Do not fall back to
# the default branch, whose push/schedule runs are exactly the source of the
# false expectation this check prevents. Instead, the first PR bootstraps from
# every check that actually reported on its reviewed SHA, with a visible WARN
# that this is weaker evidence than the normal observed baseline.
#
# For later PRs, intersect the names observed on the five most recent merged
# PR heads. A path-filtered job absent from any one of those heads drops out,
# while a universal job remains expected. If a merged head cannot be read, do
# not silently turn that observation failure into first-PR bootstrap evidence.
merged_heads=$(gh pr list --repo "$REPO" --state merged --base "$BASE" --limit 100 \
  --json headRefOid,mergedAt \
  --jq 'map(select(.mergedAt != null)) | sort_by(.mergedAt) | reverse | .[0:5] | .[].headRefOid // empty' \
  2>/dev/null || true)
baseline_count=0
baseline_fetch_failed=0
expected_names='[]'
while IFS= read -r merged_head; do
  [[ -n "$merged_head" ]] || continue
  baseline_count=$((baseline_count + 1))
  baseline_payload=$(gh api "repos/$REPO/commits/$merged_head/check-runs" --paginate 2>/dev/null) || {
    baseline_fetch_failed=1
    break
  }
  head_names=$(printf '%s' "$baseline_payload" | jq -sc '[.[].check_runs[].name] | unique' 2>/dev/null) || {
    baseline_fetch_failed=1
    break
  }
  if [[ "$baseline_count" -eq 1 ]]; then
    expected_names="$head_names"
  else
    expected_names=$(jq -nc --argjson left "$expected_names" --argjson right "$head_names" \
      '$left as $left | $right as $right | [$left[] | . as $name | select($right | index($name))]')
  fi
done <<< "$merged_heads"

latest=$(printf '%s' "$runs" | jq -c '
  group_by(.name)
  | map(sort_by(.started_at, .completed_at) | last)' 2>/dev/null)

n=$(printf '%s' "$latest" | jq -r 'length' 2>/dev/null || echo 0)
present_names=$(printf '%s' "$latest" | jq -c '[.[].name] | unique' 2>/dev/null || echo '[]')
bad=$(printf '%s' "$latest" | jq -r \
      '[.[]|select(.status!="completed" or (.conclusion|IN("success","skipped","neutral")|not))]|length' \
      2>/dev/null || echo 1)
expected_n=$(printf '%s' "$expected_names" | jq -r 'length' 2>/dev/null || echo 0)
missing=$(jq -nc --argjson expected "$expected_names" --argjson present "$present_names" \
  '$expected - $present' 2>/dev/null || echo '[]')
# A change that intentionally removes a workflow can make its historical check
# names impossible to report on the reviewed head.  The operator may provide a
# comma-separated, explicitly recorded exception; every name is printed and
# removed from the blocking set, never silently ignored.
override_raw="${GATE_ALLOW_MISSING_CHECKS:-}"
if [[ -n "$override_raw" ]]; then
  override_json=$(printf '%s' "$override_raw" | jq -Rcs 'split(",") | map(gsub("^[[:space:]]+|[[:space:]]+$"; "")) | map(select(length > 0))')
  if [[ "$override_json" != "[]" ]]; then
    overridden=$(jq -nc --argjson missing "$missing" --argjson allowed "$override_json" '$missing - $allowed')
    waived=$(jq -nc --argjson missing "$missing" --argjson allowed "$override_json" '$missing - ($missing - $allowed)')
    if [[ "$waived" != "[]" ]]; then
      echo "  WARN    operator override allows missing checks: $(printf '%s' "$waived" | jq -r 'join(", ")')"
      missing="$overridden"
    fi
  fi
fi
missing_n=$(printf '%s' "$missing" | jq -r 'length' 2>/dev/null || echo 0)
if [[ "${n:-0}" -lt 1 ]]; then
  say_bad "no checks reported yet on ${REVIEWED:0:8}"
elif [[ "${bad:-1}" != "0" ]]; then
  say_bad "checks on ${REVIEWED:0:8}: $n distinct, $bad not passing"
  printf '%s' "$latest" | jq -r \
    '.[]|select(.status!="completed" or (.conclusion|IN("success","skipped","neutral")|not))
        |"            \(.name): \(.status)/\(.conclusion // "pending")"'
elif [[ "${baseline_fetch_failed:-0}" = "1" ]]; then
  say_bad "cannot observe check runs for the merged pull-request baseline"
elif [[ "${baseline_count:-0}" -lt 1 ]]; then
  say_warn "no merged pull request baseline is available; bootstrapping from checks observed on ${REVIEWED:0:8}"
  say_ok "$n distinct checks on ${REVIEWED:0:8}, latest run of each passing (bootstrap)"
elif [[ "${expected_n:-0}" -lt 1 ]]; then
  say_bad "cannot observe a common expected check name across the last $baseline_count merged pull requests"
elif [[ "${missing_n:-0}" -gt 0 ]]; then
  say_bad "checks not yet reported on ${REVIEWED:0:8}: $(printf '%s' "$missing" | jq -r 'join(", ")')"
else
  say_ok "$n distinct checks on ${REVIEWED:0:8}, latest run of each passing"
fi

# 4. Holds. hold:author is the author mid-fix — a single label is unambiguous
#    because a PR has exactly one author lane. A review hold is not: two
#    reviewers can each have an independent open finding on the same PR, and
#    one shared `hold:review` boolean cannot tell one holder's satisfaction
#    from another's — clearing it for one clears it for both (#385). So a
#    review hold is named per holder: `hold:review:<agent>`, set and cleared
#    only by that agent (hold.sh), and every named holder present blocks the
#    merge independently. The bare `hold:review` label (pre-#385) is still
#    honored as an unattributed hold during migration, since anyone may have
#    set it under the old convention and it still means the same thing: do
#    not merge until its owner clears it.
labels=$(printf '%s' "$pr" | jq -r '[.labels[].name]|join(",")')
echo "  labels: ${labels:-none}"

case ",$labels," in
  *",hold:author,"*) say_bad "hold:author is set — the label's owner clears it, not you" ;;
  *)                  say_ok "no hold:author" ;;
esac

review_holders=$(printf '%s' "$pr" | jq -r \
  '[.labels[].name | select(startswith("hold:review:")) | ltrimstr("hold:review:")] | join(",")')
if [[ -n "$review_holders" ]]; then
  say_bad "hold:review held by $review_holders — each holder clears their own (hold.sh review clear), not you"
else
  say_ok "no named hold:review:<agent>"
fi

case ",$labels," in
  *",hold:review,"*) say_bad "hold:review (unattributed, pre-#385) is set — its owner clears it, not you" ;;
  *)                  say_ok "no unattributed hold:review" ;;
esac

# 5. Ready state and independent exact-head review. GitHub accounts are shared,
# so ordinary account identity is only corroboration: the stable **From:**
# marker must differ from the PR's one agent:<id> lane. The exact immutable
# numeric identity of a trusted GitHub App service account is the narrow
# exception: Dependabot opens a non-draft PR directly and cannot participate in
# claim.sh or rewrite its generated body. It gets the stable author lane
# "github-dependabot" without pretending a human claim exists. Titles, branches,
# display logins, and ordinary labels never qualify.
automated_author=$(ai_team_trusted_automated_author_lane "$pr_identity")
author_agents=$(printf '%s' "$pr" | jq -c \
  '[.labels[].name | select(startswith("agent:")) | ltrimstr("agent:")] | unique' \
  2>/dev/null || echo '[]')
author_count=$(printf '%s' "$author_agents" | jq -r 'length' 2>/dev/null || echo 0)
author_agent=$(printf '%s' "$author_agents" | jq -r '.[0] // ""' 2>/dev/null)

if [[ -n "$automated_author" ]]; then
  task_labels=$(printf '%s' "$pr" | jq -r \
    '[.labels[].name | select(startswith("task:"))] | join(",")')
  if [[ -n "$task_labels" ]]; then
    say_bad "trusted automated PR must not carry task:* claim metadata ($task_labels)"
  else
    say_ok "trusted automated PR is ready without task:* claim metadata"
  fi
  if [[ "$author_count" = "0" ]]; then
    author_agent="$automated_author"
    say_ok "trusted automated author is $author_agent"
  else
    say_bad "trusted automated author $automated_author must not carry agent:<id> labels"
  fi
else
  case ",$labels," in
    *",task:review,"*) say_ok "task:review is set" ;;
    *)                 say_bad "task:review is not set — the author has not handed off a final head" ;;
  esac
  if [[ "$author_count" = "1" ]]; then
    say_ok "author lane is agent:$author_agent"
  else
    say_bad "expected exactly one agent:<id> author lane, found $author_count"
  fi
fi

# 5b. Issue-closing reference (#354). claim.sh / ready.sh write Closes #N; the
# gate refuses a handoff that dropped it so merge cannot leave the board lying.
# Identity rules live in _lane_issue.sh (shared with ready.sh): trailing (#N),
# branch issue-N, fail closed on conflict. Deliberate issue-less path: exact
# body line `AI-Team-Lane-Issue: none` when neither title nor branch names an issue.
title=$(printf '%s' "$pr" | jq -r '.title // ""')
branch=$(printf '%s' "$pr" | jq -r '.headRefName // ""')
pr_body=$(printf '%s' "$pr" | jq -r '.body // ""')
if [ -n "$automated_author" ]; then
  lane_issue="none"
else
  lane_issue=$(ai_team_derive_lane_issue "$title" "$branch" "$pr_body" "${READY_ISSUE:-}")
fi
# What GitHub will actually close on merge, which is not the same question as
# what the body says (#67). `closingIssuesReferences` is populated by body
# keywords AND by the Development panel, and a panel link leaves no trace in
# the body at all — so a PR can be truthfully documented as closing nothing and
# still close an issue. Reconcile the declared lane against the field GitHub
# acts on, or the board learns about a closure after it has happened.
closing_refs=$(printf '%s' "$pr" | jq -r '[.closingIssuesReferences[]?.number] | unique | map(tostring) | join(" ")')

case "$lane_issue" in
  error:*)
    say_bad "${lane_issue#error:}"
    ;;
  none)
    if [ -n "$closing_refs" ]; then
      say_bad "lane declares no issue but GitHub will close #${closing_refs// /, #} on merge — declare the lane or unlink it in the Development panel"
    elif [ -n "$automated_author" ]; then
      say_ok "issue-less trusted automated lane"
    else
      say_ok "issue-less lane (AI-Team-Lane-Issue: none)"
    fi
    ;;
  *)
    if ai_team_body_has_closing_reference "$pr_body" "$lane_issue"; then
      say_ok "body closes #$lane_issue"
    else
      say_bad "body has no closing reference to #$lane_issue — run ready.sh or add Closes #$lane_issue"
    fi
    if [ -n "$closing_refs" ] && [ " $closing_refs " != " $lane_issue " ]; then
      say_bad "lane is #$lane_issue but GitHub will close #${closing_refs// /, #} on merge"
    fi
    ;;
esac

# A pure, verifiable subtree pull is a trusted SHAPE (#61): its content
# already passed the package repository's own gate upstream, so no
# adopter-side agent review is required. The adopter opts in by committing
# `.ai-team/subtree-pull` containing `<upstream-repo> <branch> <prefix>`;
# the same shape decision runs in CI, so the two paths cannot disagree. Any
# non-trusted outcome — including "cannot judge" — falls through to the
# ordinary review requirement below.
trusted_pull=0
if [[ -r "$ROOT/.ai-team/subtree-pull" ]]; then
  read -r pull_upstream pull_branch pull_prefix < "$ROOT/.ai-team/subtree-pull" || true
  if [[ -n "${pull_upstream:-}" && -n "${pull_branch:-}" && -n "${pull_prefix:-}" ]]; then
    pull_base=$(git merge-base "origin/$BASE" "$REVIEWED" 2>/dev/null)
    if [[ -n "$pull_base" ]] && "$here/subtree_pull_gate.sh" \
        "$pull_base" "$REVIEWED" "$pull_prefix" "$pull_upstream" "$pull_branch" >/dev/null 2>&1; then
      trusted_pull=1
    fi
  fi
fi

# The review grammar has one canonical implementation. Both this local
# pre-flight and the required CI workflow call review_gate.sh, so an author
# cannot get a different verdict by switching landing paths.
review_exit=0
if [[ "$trusted_pull" == "1" ]]; then
  review_output="PASS: trusted subtree pull — content reviewed upstream behind the package gate (#61)"
else
  review_output=$("$here/review_gate.sh" "$PR" "$REVIEWED" 2>&1) || review_exit=$?
fi
while IFS= read -r review_line; do
  [[ -n "$review_line" ]] || continue
  case "$review_line" in
    "PASS: "*) say_ok "${review_line#PASS: }" ;;
    "FAIL: "*) say_bad "${review_line#FAIL: }" ;;
    "WARN: "*) say_warn "${review_line#WARN: }" ;;
    "ERROR: "*) say_bad "review gate: ${review_line#ERROR: }" ;;
    *)         say_warn "review gate: $review_line" ;;
  esac
done <<< "$review_output"
if [[ "$review_exit" -gt 1 ]]; then
  say_bad "review gate could not evaluate the PR"
fi

# Native GitHub approvals are an adopter's external repository protection, not
# a second identity format the shared AI Team account can honestly manufacture.
# They do not change this gate's verdict about AI Team evidence, but the gate
# can make an unmet native requirement visible before land.sh reaches a refused
# merge endpoint (#35). A failed inspection remains a warning: this preflight
# must not turn an unavailable GitHub rules endpoint into a false AI Team block.
branch_rules=""
if branch_rules=$(gh api "repos/$REPO/rules/branches/$BASE" --paginate 2>/dev/null); then
  required_native_approvals=$(printf '%s' "$branch_rules" | jq -s \
    '[.[][]? | select(.type == "pull_request") | (.parameters.required_approving_review_count? // 0)] | max // 0' \
    2>/dev/null)
  case "$required_native_approvals" in
    ''|*[!0-9]*)
      say_warn "cannot parse GitHub native approval rules for $BASE; a passing AI Team gate does not predict external merge permission"
      ;;
    0)
      say_ok "no GitHub native approval requirement reported for $BASE"
      ;;
    *)
      native_reviews=""
      if native_reviews=$(gh api "repos/$REPO/pulls/$PR/reviews" --paginate 2>/dev/null); then
        native_approved=$(printf '%s' "$native_reviews" | jq -s \
          '[.[][]?
            | select(.user.login? != null)
            | {login: .user.login, state: (.state // ""), submitted_at: (.submitted_at // ""), id: (.id // 0)}]
           | group_by(.login)
           | map(sort_by(.submitted_at, .id) | last | select(.state == "APPROVED"))
           | length' 2>/dev/null)
        case "$native_approved" in
          ''|*[!0-9]*)
            say_warn "cannot parse native GitHub reviews; a passing AI Team gate does not predict external merge permission"
            ;;
          *)
            if [[ "$native_approved" -lt "$required_native_approvals" ]]; then
              say_warn "GitHub requires $required_native_approvals native approval(s) on $BASE, but only $native_approved distinct current APPROVED reviewer(s) are visible — a separate eligible native reviewer or automation is still required before merge"
            else
              say_ok "GitHub native approval preflight: requires $required_native_approvals, $native_approved distinct current APPROVED reviewer(s) visible; GitHub still decides eligibility and freshness"
            fi
            ;;
        esac
      else
        say_warn "cannot inspect native GitHub reviews; a passing AI Team gate does not predict external merge permission"
      fi
      ;;
  esac
else
  say_warn "cannot inspect GitHub native approval rules for $BASE; a passing AI Team gate does not predict external merge permission"
fi

# Keep the comment-stream diagnostic below focused on the case where a review
# was not already accepted. Comments are informational only and never count.
accepted_agents=""
if grep -qE '^PASS: (independent exact-head acceptance|trusted subtree pull)' <<< "$review_output"; then
  accepted_agents="present"
elif ! grep -q '^FAIL:' <<< "$review_output"; then
  # A malformed or partially shipped delegate must not turn the review
  # dimension into silence. Gate success requires affirmative acceptance.
  say_bad "review gate did not report an independent exact-head acceptance"
fi

# 5d. gh pr review --approve is refused on our own PRs (shared account), and
# the natural fallback `gh pr comment` posts fine but gate.sh only ever reads
# repos/:repo/pulls/:pr/reviews. A comment-stream marker never becomes a
# verdict, but a blocking marker must remain visible even when another reviewer
# has already accepted: otherwise the acceptance hides the warning (#392).
issue_comments=$(gh api "repos/$REPO/issues/$PR/comments" --paginate 2>/dev/null \
  | jq -s 'add // []' 2>/dev/null)
[[ -n "$issue_comments" ]] || issue_comments='[]'

stray_blocking_agents=$(printf '%s' "$issue_comments" | jq -r --arg author "$author_agent" '
  def from_agent:
    ([((.body // "") | split("\n")[]
       | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<agent>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").agent
       | ascii_downcase)] | unique) as $agents
    | if ($agents | length) == 1 then $agents[0] else "" end;
  def has_blocking_marker:
    # Request changes is the GitHub word for changes required (#70).
    (.body // "") | test("\\*\\*Verdict:\\*\\*[[:space:]]*(?:changes required|request changes)(?:[[:space:]]|$)"; "i");
  [.[] | . + {agent: from_agent} | select(.agent != "" and .agent != $author and has_blocking_marker) | .agent]
  | unique | join("\n")
' 2>/dev/null)

if [[ -n "$stray_blocking_agents" ]]; then
  while IFS= read -r agent; do
    [[ -n "$agent" ]] || continue
    say_warn "found a blocking verdict marker from $agent in the comment stream; gate reads reviews only — repost with 'gh pr review --comment'"
  done <<< "$stray_blocking_agents"
fi

if [[ -z "$accepted_agents" ]]; then
  stray_accept_agents=$(printf '%s' "$issue_comments" | jq -r --arg author "$author_agent" '
    def from_agent:
      ([((.body // "") | split("\n")[]
         | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<agent>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").agent
         | ascii_downcase)] | unique) as $agents
      | if ($agents | length) == 1 then $agents[0] else "" end;
    # Deliberately unanchored, unlike explicit_verdicts/5c: this path only ever
    # produces a WARN, never an acceptance, so a missed warning (silence) is
    # the failure that matters and a false one costs nothing — someone posted
    # a well-formed verdict gets told to repost, which they can ignore. An
    # agent improvising the channel (gh pr comment, after --approve is
    # refused) improvises the formatting too: the observed incident was
    # exactly this, "**From:** opus-5 — **Verdict:** accept at `sha`." on one
    # line, which a line-anchored **Verdict:** would never match.
    def has_accept_marker:
      # Approve is the GitHub word for accept (#70).
      (.body // "") | test("\\*\\*Verdict:\\*\\*[[:space:]]*(?:accept(?: with follow-up)?|approve)(?:[[:space:]]|$)"; "i");
    [.[] | . + {agent: from_agent} | select(.agent != "" and .agent != $author and has_accept_marker) | .agent]
    | unique | join("\n")
  ' 2>/dev/null)

  if [[ -n "$stray_accept_agents" ]]; then
    while IFS= read -r agent; do
      [[ -n "$agent" ]] || continue
      say_warn "found a verdict marker from $agent in the comment stream; gate reads reviews only — repost with 'gh pr review --comment'"
    done <<< "$stray_accept_agents"
  fi
fi

# 6. The head has not moved since the review. GitHub's PR head also lags a push
#    by minutes, so compare the branch ref too.
if [[ "$remote_head" = "$REVIEWED" ]]; then
  say_ok "PR head is the reviewed SHA"
else
  # Abbreviations were resolved to a full SHA up front, so this comparison is
  # deliberately exact: what merges must be exactly what was verified.
  say_bad "PR head is $remote_head but you reviewed $REVIEWED — re-review the new head"
fi
# Only meaningful while the PR is open: the branch is normally deleted on merge,
# and that 404 means "merged", not "diverged".
if [[ "$state" = "OPEN" ]]; then
  branch=$(printf '%s' "$pr" | jq -r .headRefName)
  ref=$(gh api "repos/$REPO/git/refs/heads/$branch" --jq .object.sha 2>/dev/null)
  case "$ref" in
    [0-9a-f][0-9a-f]*)
      [[ "$ref" = "$remote_head" ]] \
        || say_bad "branch $branch is at ${ref:0:8} but the PR head says ${remote_head:0:8} — a push has not propagated yet" ;;
    *) echo "  note: no branch ref for $branch (deleted, or a fork)" ;;
  esac
fi

# 7. Something to merge at all. Our claim protocol is an empty draft PR, so every
#    claim starts as exactly this shape; #450 was taken out of draft and labelled
#    task:review, and every other check passed it (#453). Zero changed files is
#    the unambiguous case -- a mode-only or rename change still reports files.
# awk rather than `paste | bc`: bc is not installed everywhere, and its absence
# was silent -- an empty $files fell through to the zero branch and accused a
# healthy PR of being an empty claim (#598). END{print s+0} also yields 0 rather
# than nothing on no input, so the check below stands on its own.
files=$(gh api "repos/$REPO/pulls/$PR/files" --paginate --jq 'length' 2>/dev/null | awk '{s+=$1} END{print s+0}')
if [[ "${files:-0}" -eq 0 ]] 2>/dev/null; then
  say_bad "no changed files — an empty PR is a claim, not a deliverable"
else
  say_ok "$files changed file(s)"
fi

# 8. Conflicts. mergeStateStatus is permanently BLOCKED for us and carries no
#    information; mergeable does.
mergeable=$(printf '%s' "$pr" | jq -r .mergeable)
[[ "$mergeable" = "CONFLICTING" ]] && say_bad "CONFLICTING with the base branch" || say_ok "mergeable: $mergeable"

# 9. Not a check — the last word on the PR, so a hold written as prose by
#    somebody who did not know about the label is still in front of you.
echo "  --- last 3 comments ---"
gh pr view "$PR" --repo "$REPO" --json comments \
  --jq '.comments[-3:][]|"            \(.createdAt) \(.author.login): \(.body[0:100]|gsub("\n";" "))"' 2>/dev/null

if [[ "$fail" = "0" ]]; then
  echo "GATE: PASS"
else
  echo "GATE: FAIL"
fi
exit "$fail"
