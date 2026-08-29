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
#   docs/ai-team/scripts/gate.sh 408 abc1234 \
#     && gh api -X PUT "repos/$REPO/pulls/408/merge" -f merge_method=merge
#
# Never put the checks and the merge inside one compound command where the merge
# can still run when a check fails. That is exactly how #382 reached main while
# BEHIND it: the check printed its warning and the merge went ahead anyway.
#
# Why both: Protect Main now requires the six repository/Sonar contexts with
# strict_required_status_checks_policy and no merge bypass actors. This gate is
# still the richer pre-flight: it checks exact reviewed-head identity, lane
# ownership, holds, issue closure, and independent review before a merge call.
#
set -u

PR="${1:-}"
if [ -z "$PR" ]; then
  echo "usage: gate.sh <pr-number> [<reviewed-sha>]" >&2
  exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=docs/ai-team/scripts/_lane_issue.sh
# shellcheck disable=SC1091
source "$here/_lane_issue.sh"
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$here/_default_branch.sh"
BASE=$(ai_team_default_branch)

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git checkout" >&2; exit 2; }
cd "$ROOT" || exit 2

REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)
[ -n "$REPO" ] || { echo "cannot resolve the repository from gh" >&2; exit 2; }

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
[ "$origin_repo" = "$REPO" ] || {
  echo "origin is '$origin_url' but gh resolves the repository as '$REPO'." >&2
  echo "The gate proves containment against origin/$BASE, so origin must be the" >&2
  echo "canonical repository. Run from a clone whose origin is $REPO." >&2
  exit 2
}

# One fetch of PR state; every check below reads from it.
pr=$(gh pr view "$PR" --repo "$REPO" \
       --json headRefOid,headRefName,title,body,isDraft,state,mergeable,labels 2>/dev/null)
[ -n "$pr" ] || { echo "cannot read PR #$PR from $REPO" >&2; exit 2; }

remote_head=$(printf '%s' "$pr" | jq -r .headRefOid)

REVIEWED="${2:-}"
if [ -z "$REVIEWED" ]; then
  REVIEWED="$remote_head"
  echo "note: no reviewed SHA given — gating the current head $REVIEWED."
  echo "      Pass the SHA you actually reviewed, so a push after your review fails this gate."
elif [ "${#REVIEWED}" -lt 40 ]; then
  # An abbreviation is only usable once the canonical repository resolves it
  # to exactly one commit; every later comparison and check-run query then
  # uses that full SHA, so the merged-is-verified contract stays exact.
  if [ "${#REVIEWED}" -lt 12 ]; then
    echo "reviewed SHA '$REVIEWED' is too short (<12 chars) to identify a commit safely — pass at least 12, ideally all 40." >&2
    exit 2
  fi
  resolved=$(gh api "repos/$REPO/commits/$REVIEWED" --jq .sha 2>/dev/null)
  if [ "${#resolved}" -ne 40 ] || printf '%s' "$resolved" | grep -q '[^0-9a-f]'; then
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
[ "$state" = "OPEN" ] && say_ok "state is OPEN" || say_bad "state is $state"
[ "$draft" = "false" ] && say_ok "not a draft" || say_bad "PR is a DRAFT — never merge someone's claim"

# 2. Up to date with main. CI green on a tree that never existed on main is not
#    evidence about main. #326 landed red exactly this way.
if [ "$reviewed_object_available" != "1" ]; then
  say_bad "reviewed SHA $REVIEWED is unavailable after fetching PR #$PR — its history may have been rewritten; re-review the current head"
elif git merge-base --is-ancestor "origin/$BASE" "$REVIEWED" 2>/dev/null; then
  say_ok "contains origin/$BASE ($(git rev-parse --short "origin/$BASE"))"
else
  say_bad "BEHIND origin/$BASE ($(git rev-parse --short "origin/$BASE")) — merge $BASE into the branch first"
fi

# 3. Checks on the REVIEWED sha, not on the PR, not on the branch. The current
#    main tip supplies the expected check names. This is observed repository
#    state, not a count copied into the script; when CI adds or removes a job,
#    the gate follows main. A passing early check can therefore never authorize
#    a merge while another expected check has not reported on the reviewed SHA.
# Judge the LATEST run of each check NAME, not every run on the SHA. A
# superseded run stays on the commit forever: `concurrency: cancel-in-progress`
# leaves a `cancelled` entry behind whenever a PR is force-pushed or pushed
# twice quickly, and counting it blocked #432 while all four of those checks had
# already passed on the same SHA (#433). `neutral` is likewise not a failure --
# CodeQL reports it transiently before settling.
runs=$(gh api "repos/$REPO/commits/$REVIEWED/check-runs" --paginate 2>/dev/null \
  | jq -sc '[.[].check_runs[]]')

main_sha=$(git rev-parse "origin/$BASE")
main_runs=$(gh api "repos/$REPO/commits/$main_sha/check-runs" --paginate 2>/dev/null \
  | jq -sc '[.[].check_runs[]]')

latest=$(printf '%s' "$runs" | jq -c '
  group_by(.name)
  | map(sort_by(.started_at, .completed_at) | last)' 2>/dev/null)

expected_latest=$(printf '%s' "$main_runs" | jq -c '
  group_by(.name)
  | map(sort_by(.started_at, .completed_at) | last)' 2>/dev/null)

n=$(printf '%s' "$latest" | jq -r 'length' 2>/dev/null || echo 0)
expected_n=$(printf '%s' "$expected_latest" | jq -r 'length' 2>/dev/null || echo 0)
present_names=$(printf '%s' "$latest" | jq -c '[.[].name] | unique' 2>/dev/null || echo '[]')
expected_names=$(printf '%s' "$expected_latest" | jq -c '[.[].name] | unique' 2>/dev/null || echo '[]')
missing=$(jq -nc --argjson expected "$expected_names" --argjson present "$present_names" \
  '$expected - $present' 2>/dev/null || echo '[]')
missing_n=$(printf '%s' "$missing" | jq -r 'length' 2>/dev/null || echo 0)
bad=$(printf '%s' "$latest" | jq -r \
      '[.[]|select(.status!="completed" or (.conclusion|IN("success","skipped","neutral")|not))]|length' \
      2>/dev/null || echo 1)
if [ "${expected_n:-0}" -lt 1 ]; then
  say_bad "cannot observe expected checks on origin/$BASE ${main_sha:0:8}"
elif [ "${n:-0}" -lt 1 ]; then
  say_bad "no checks reported yet on ${REVIEWED:0:8}"
elif [ "${missing_n:-0}" -gt 0 ]; then
  say_bad "checks not yet reported on ${REVIEWED:0:8}: $(printf '%s' "$missing" | jq -r 'join(", ")')"
elif [ "${bad:-1}" != "0" ]; then
  say_bad "checks on ${REVIEWED:0:8}: $n distinct, $bad not passing"
  printf '%s' "$latest" | jq -r \
    '.[]|select(.status!="completed" or (.conclusion|IN("success","skipped","neutral")|not))
        |"            \(.name): \(.status)/\(.conclusion // "pending")"'
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
if [ -n "$review_holders" ]; then
  say_bad "hold:review held by $review_holders — each holder clears their own (hold.sh review clear), not you"
else
  say_ok "no named hold:review:<agent>"
fi

case ",$labels," in
  *",hold:review,"*) say_bad "hold:review (unattributed, pre-#385) is set — its owner clears it, not you" ;;
  *)                  say_ok "no unattributed hold:review" ;;
esac

# 5. Ready state and independent exact-head review. GitHub accounts are shared,
# so account identity is only corroboration: the stable **From:** marker must
# differ from the PR's one agent:<id> lane. Native APPROVED reviews count; a
# shared-account COMMENTED review carries an explicit **Verdict:** accept marker.
case ",$labels," in
  *",task:review,"*) say_ok "task:review is set" ;;
  *)                 say_bad "task:review is not set — the author has not handed off a final head" ;;
esac

author_agents=$(printf '%s' "$pr" | jq -c \
  '[.labels[].name | select(startswith("agent:")) | ltrimstr("agent:")] | unique' \
  2>/dev/null || echo '[]')
author_count=$(printf '%s' "$author_agents" | jq -r 'length' 2>/dev/null || echo 0)
author_agent=$(printf '%s' "$author_agents" | jq -r '.[0] // ""' 2>/dev/null)
if [ "$author_count" = "1" ]; then
  say_ok "author lane is agent:$author_agent"
else
  say_bad "expected exactly one agent:<id> author lane, found $author_count"
fi

# 5b. Issue-closing reference (#354). claim.sh / ready.sh write Closes #N; the
# gate refuses a handoff that dropped it so merge cannot leave the board lying.
# Identity rules live in _lane_issue.sh (shared with ready.sh): trailing (#N),
# branch issue-N, fail closed on conflict. Deliberate issue-less path: exact
# body line `AI-Team-Lane-Issue: none` when neither title nor branch names an issue.
title=$(printf '%s' "$pr" | jq -r '.title // ""')
branch=$(printf '%s' "$pr" | jq -r '.headRefName // ""')
pr_body=$(printf '%s' "$pr" | jq -r '.body // ""')
lane_issue=$(ai_team_derive_lane_issue "$title" "$branch" "$pr_body" "")
case "$lane_issue" in
  error:*)
    say_bad "${lane_issue#error:}"
    ;;
  none)
    say_ok "issue-less lane (AI-Team-Lane-Issue: none)"
    ;;
  *)
    if ai_team_body_has_closing_reference "$pr_body" "$lane_issue"; then
      say_ok "body closes #$lane_issue"
    else
      say_bad "body has no closing reference to #$lane_issue — run ready.sh or add Closes #$lane_issue"
    fi
    ;;
esac

# `gh api --paginate` prints one JSON array per page. Slurp and flatten those
# pages before deriving the latest machine verdict for each stable reviewer.
reviews=$(gh api "repos/$REPO/pulls/$PR/reviews" --paginate 2>/dev/null \
  | jq -s 'add // []' 2>/dev/null)
[ -n "$reviews" ] || reviews='[]'

latest_reviews=$(printf '%s' "$reviews" | jq -c --arg sha "$REVIEWED" '
  def from_agent:
    ([((.body // "") | split("\n")[]
       | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<agent>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").agent
       | ascii_downcase)] | unique) as $agents
    | if ($agents | length) == 1 then $agents[0] else "" end;
  def explicit_verdicts:
    [((.body // "") | split("\n")[]
       | capture("^\\*\\*Verdict:\\*\\*[[:space:]]*(?<verdict>accept(?: with follow-up)?|changes required)[[:space:]]*$"; "i").verdict
       | ascii_downcase)] | unique;
  [.[]
   | select(.commit_id == $sha)
   | . + {agent: from_agent, explicit_verdicts: explicit_verdicts}
   | . + {explicit_verdict:
       (if (.explicit_verdicts | length) == 1
        then .explicit_verdicts[0]
        else ""
        end)}
   | . + {verdict:
       (if .state == "DISMISSED"
        then ""
        elif .state == "CHANGES_REQUESTED"
        then "changes required"
        elif (.explicit_verdicts | length) > 1
        then ""
        elif .explicit_verdict == "changes required"
        then "changes required"
        elif .state == "APPROVED"
             or .explicit_verdict == "accept"
             or .explicit_verdict == "accept with follow-up"
        then "accept"
        else ""
        end)}
   | select(.agent != "")]
  | sort_by(.agent, .submitted_at, .id)
  | group_by(.agent)
  | map(last)
' 2>/dev/null || echo '[]')

accepted_agents=$(printf '%s' "$latest_reviews" | jq -r --arg author "$author_agent" \
  '[.[] | select(.agent != $author and .verdict == "accept") | .agent] | unique | join(",")' \
  2>/dev/null)
blocking_agents=$(printf '%s' "$latest_reviews" | jq -r --arg author "$author_agent" \
  '[.[] | select(.agent != $author and .verdict == "changes required") | .agent] | unique | join(",")' \
  2>/dev/null)

if [ -n "$accepted_agents" ]; then
  say_ok "independent exact-head acceptance from $accepted_agents"
else
  say_bad "no independent exact-head acceptance; require **From:** <reviewer> plus APPROVED or **Verdict:** accept"
fi
if [ -z "$blocking_agents" ]; then
  say_ok "no independent exact-head changes-required verdict"
else
  say_bad "independent exact-head changes required by $blocking_agents"
fi

# 5c. A review that carries a **From:** marker but no line-anchored **Verdict:**
# (or 2+ conflicting ones) is silently excluded above rather than counted — say
# so, so a reviewer who wrote an inline verdict finds out from the gate instead
# of a "no acceptance" message that looks identical to never having reviewed (#359).
malformed_agents=$(printf '%s' "$latest_reviews" | jq -r --arg author "$author_agent" \
  '[.[] | select(.agent != $author and .verdict == "") | .agent] | unique | join("\n")' \
  2>/dev/null)
if [ -n "$malformed_agents" ]; then
  while IFS= read -r agent; do
    [ -n "$agent" ] || continue
    say_warn "a review marker from $agent was seen at ${REVIEWED:0:8} but rejected for format — **Verdict:** must stand alone on its own line (accept / accept with follow-up / changes required)"
  done <<< "$malformed_agents"
fi

# 5d. gh pr review --approve is refused on our own PRs (shared account), and
# the natural fallback `gh pr comment` posts fine but gate.sh only ever reads
# repos/:repo/pulls/:pr/reviews. A comment-stream marker never becomes a
# verdict, but a blocking marker must remain visible even when another reviewer
# has already accepted: otherwise the acceptance hides the warning (#392).
issue_comments=$(gh api "repos/$REPO/issues/$PR/comments" --paginate 2>/dev/null \
  | jq -s 'add // []' 2>/dev/null)
[ -n "$issue_comments" ] || issue_comments='[]'

stray_blocking_agents=$(printf '%s' "$issue_comments" | jq -r --arg author "$author_agent" '
  def from_agent:
    ([((.body // "") | split("\n")[]
       | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<agent>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").agent
       | ascii_downcase)] | unique) as $agents
    | if ($agents | length) == 1 then $agents[0] else "" end;
  def has_blocking_marker:
    (.body // "") | test("\\*\\*Verdict:\\*\\*[[:space:]]*changes required(?:[[:space:]]|$)"; "i");
  [.[] | . + {agent: from_agent} | select(.agent != "" and .agent != $author and has_blocking_marker) | .agent]
  | unique | join("\n")
' 2>/dev/null)

if [ -n "$stray_blocking_agents" ]; then
  while IFS= read -r agent; do
    [ -n "$agent" ] || continue
    say_warn "found a blocking verdict marker from $agent in the comment stream; gate reads reviews only — repost with 'gh pr review --comment'"
  done <<< "$stray_blocking_agents"
fi

if [ -z "$accepted_agents" ]; then
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
      (.body // "") | test("\\*\\*Verdict:\\*\\*[[:space:]]*accept(?: with follow-up)?(?:[[:space:]]|$)"; "i");
    [.[] | . + {agent: from_agent} | select(.agent != "" and .agent != $author and has_accept_marker) | .agent]
    | unique | join("\n")
  ' 2>/dev/null)

  if [ -n "$stray_accept_agents" ]; then
    while IFS= read -r agent; do
      [ -n "$agent" ] || continue
      say_warn "found a verdict marker from $agent in the comment stream; gate reads reviews only — repost with 'gh pr review --comment'"
    done <<< "$stray_accept_agents"
  fi
fi

# 6. The head has not moved since the review. GitHub's PR head also lags a push
#    by minutes, so compare the branch ref too.
if [ "$remote_head" = "$REVIEWED" ]; then
  say_ok "PR head is the reviewed SHA"
else
  # Abbreviations were resolved to a full SHA up front, so this comparison is
  # deliberately exact: what merges must be exactly what was verified.
  say_bad "PR head is $remote_head but you reviewed $REVIEWED — re-review the new head"
fi
# Only meaningful while the PR is open: the branch is normally deleted on merge,
# and that 404 means "merged", not "diverged".
if [ "$state" = "OPEN" ]; then
  branch=$(printf '%s' "$pr" | jq -r .headRefName)
  ref=$(gh api "repos/$REPO/git/refs/heads/$branch" --jq .object.sha 2>/dev/null)
  case "$ref" in
    [0-9a-f][0-9a-f]*)
      [ "$ref" = "$remote_head" ] \
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
if [ "${files:-0}" -eq 0 ] 2>/dev/null; then
  say_bad "no changed files — an empty PR is a claim, not a deliverable"
else
  say_ok "$files changed file(s)"
fi

# 8. Conflicts. mergeStateStatus is permanently BLOCKED for us and carries no
#    information; mergeable does.
mergeable=$(printf '%s' "$pr" | jq -r .mergeable)
[ "$mergeable" = "CONFLICTING" ] && say_bad "CONFLICTING with the base branch" || say_ok "mergeable: $mergeable"

# 9. Not a check — the last word on the PR, so a hold written as prose by
#    somebody who did not know about the label is still in front of you.
echo "  --- last 3 comments ---"
gh pr view "$PR" --repo "$REPO" --json comments \
  --jq '.comments[-3:][]|"            \(.createdAt) \(.author.login): \(.body[0:100]|gsub("\n";" "))"' 2>/dev/null

if [ "$fail" = "0" ]; then
  echo "GATE: PASS"
else
  echo "GATE: FAIL"
fi
exit "$fail"
