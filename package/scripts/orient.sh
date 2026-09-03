#!/usr/bin/env bash
#
# Everything a teammate needs to start working, in one command.
#
#   docs/ai-team/scripts/orient.sh
#
# This exists because orientation is our largest repeated cost: every agent that
# starts pays for it, and the short-lived ones pay for it once per task. Prose
# cannot tell you who holds a file right now; this can.
#
set -u

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git checkout" >&2; exit 2; }
cd "$ROOT" || exit 2
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=docs/ai-team/scripts/_lane_issue.sh
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_lane_issue.sh"
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_default_branch.sh"
BASE=$(ai_team_default_branch)
# Exported so the project hook (#8) can use the branch orient.sh already
# resolved instead of re-sourcing _default_branch.sh — a hook copied out to
# .ai-team/ at the repository root has no relative path back to package/scripts/.
export AI_TEAM_DEFAULT_BRANCH="$BASE"

# A halt must reach every agent regardless of tool, so it lives on the board and
# surfaces here — the one command every agent runs each tick. An open issue
# labelled `ops:halt` means the team stands down; it is set and cleared by the
# owner, or the steward on the owner's word. Printed first so a stand-down that
# went out on one tool's private channel is not missed by agents on another.
if ! REPO=$(ai_team_origin_repo) || [ -z "$REPO" ]; then
  echo "== operations =="
  echo "  *** HALT STATUS UNKNOWN — STAND DOWN ***"
  echo "  Cannot resolve this repository from origin; do not claim new work."
  exit 2
fi

"$SCRIPT_DIR/halt_status.sh" "$REPO"
halt_status=$?
[ "$halt_status" -eq 0 ] || exit "$halt_status"

echo
echo "== active leader/steward =="
# `ops:steward` is never removed on retirement (#383) — it stays on the
# appointment issue as the durable historical record, so an all-state query
# returns every past steward alongside the current one by design. `--state
# open` is what makes an appointment count as active; it is the whole
# contract, not an incidental filter, so do not drop it while refactoring.
stewards=$(gh issue list --repo "$REPO" --state open --label "ops:steward" \
  --json number,title,labels \
  --jq '.[]
        | ([.labels[]?.name | select(startswith("agent:"))]) as $agents
        | [(.number | tostring), ($agents | if length == 0 then "-" else join(", ") end), ($agents | length | tostring), .title]
        | @tsv' \
  2>/dev/null)
steward_status=$?
if [ "$steward_status" -ne 0 ]; then
  echo "  unavailable — inspect the board before relying on steward backstops"
elif [ -z "$stewards" ]; then
  echo "  none appointed"
  echo "  WARNING expected exactly one active ops:steward issue (found 0)"
else
  steward_count=$(printf '%s\n' "$stewards" | awk 'NF {count++} END {print count + 0}')
  steward_identity_note=""
  while IFS=$'\t' read -r steward_number agent_names agent_count steward_title; do
    [ -n "$steward_number" ] || continue
    if [ "$agent_count" -eq 1 ]; then
      printf '  #%s [%s] %s\n' "$steward_number" "$agent_names" "$steward_title"
      if [ "$steward_count" -eq 1 ]; then
        steward_identity_note="$steward_number|$agent_names"
      fi
    elif [ "$agent_count" -eq 0 ]; then
      printf '  #%s [MISSING agent label] %s\n' "$steward_number" "$steward_title"
      printf '  WARNING #%s active steward must carry exactly one agent:* label (found 0)\n' "$steward_number"
    else
      printf '  #%s [%s] %s\n' "$steward_number" "$agent_names" "$steward_title"
      printf '  WARNING #%s active steward must carry exactly one agent:* label (found %s)\n' "$steward_number" "$agent_count"
    fi
  done <<< "$stewards"
  if [ "$steward_count" -ne 1 ]; then
    echo "  WARNING expected exactly one active ops:steward issue (found $steward_count)"
  elif [ -n "$steward_identity_note" ]; then
    _steward_issue="${steward_identity_note%%|*}"
    _steward_agent="${steward_identity_note#*|}"
    echo "  NOTE: $_steward_agent on #$_steward_issue is the APPOINTMENT, not your **From:** identity."
    echo "        Set CLAIM_AGENT to your stable id before posting. Never borrow the appointee's id."
  fi
fi

echo
echo "== main =="
git fetch -q origin "$BASE" 2>/dev/null
echo "  origin/$BASE  $(git log "origin/$BASE" --oneline -1)"
branch=$(git rev-parse --abbrev-ref HEAD)
if git merge-base --is-ancestor "origin/$BASE" HEAD 2>/dev/null; then
  echo "  $branch: contains origin/$BASE"
else
  if [ "$branch" = "$BASE" ]; then
    behind_count=$(git rev-list --count "HEAD..origin/$BASE" 2>/dev/null)
    commit_word="commits"
    [ "$behind_count" -eq 1 ] && commit_word="commit"
    echo "  $BASE: BEHIND origin/$BASE by $behind_count $commit_word — scripts and files you read here are stale"
  else
    echo "  $branch: BEHIND origin/$BASE — merge it in before you ask anyone to review"
  fi
fi

# The project hook lives OUTSIDE the mount (#8): a copy vendored inside
# docs/ai-team/ can never be byte-identical to upstream, so `git diff` cannot
# tell "in sync" from "drifted" and `git subtree pull` conflicts on it every
# time this package changes its own project-orient.sh. An adopter-owned file
# at .ai-team/ has neither problem, and AI_TEAM_PROJECT_ORIENT lets a checkout
# that cannot use that exact path point elsewhere.
project_hook=""
for candidate in "${AI_TEAM_PROJECT_ORIENT:-}" "$ROOT/.ai-team/project-orient.sh"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  project_hook="$candidate"
  break
done
if [ -n "$project_hook" ]; then
  echo
  "$project_hook"
else
  echo
  echo "note: no project hook at .ai-team/project-orient.sh (see docs/ai-team/templates/)"
fi

echo
echo "== open pull requests — who holds what =="
gh pr list --repo "$REPO" --state open --limit 40 \
  --json number,title,isDraft,labels,headRefName \
  --jq '.[]|"  #\(.number) \(if .isDraft then "[draft]" else "        " end) \(.title[0:62])
        \(.headRefName)  \([.labels[].name]|join(" "))"' 2>/dev/null \
  || echo "  (gh unavailable)"

echo
echo "== half-claims — a lane exists but the board does not show it =="
# claim.sh can be interrupted between creating the PR and applying the labels.
# Both halves of the evidence are already on this page — an open PR carrying no
# agent:* label, and an issue that still reads unclaimed — and nothing joined
# them, so a lane on #1 sat invisible until a steward noticed by eye (#19).
# The lane issue comes from the shared contract in _lane_issue.sh rather than a
# second parser, and the owner comes from the PR body's **From:** marker, the
# same source claim.sh uses to decide whether a half-claim is yours to finish.
unlabelled=$(gh pr list --repo "$REPO" --state open --limit 40 --json number,labels \
  --jq '.[]|select([.labels[].name|select(startswith("agent:"))]|length == 0)|.number' 2>/dev/null)
half_claims=0
for half_pr in $unlabelled; do
  half_detail=$(gh pr view "$half_pr" --repo "$REPO" --json title,body,headRefName 2>/dev/null) || continue
  half_title=$(printf '%s' "$half_detail" | jq -r '.title // ""')
  half_body=$(printf '%s' "$half_detail" | jq -r '.body // ""')
  half_branch=$(printf '%s' "$half_detail" | jq -r '.headRefName // ""')
  half_issue=$(ai_team_derive_lane_issue "$half_title" "$half_branch" "$half_body" "")
  case "$half_issue" in
    ''|none|error:*) continue ;;
  esac
  # Only a lane the board is not already showing. An issue that carries its own
  # agent:* label is claimed, whatever the PR looks like.
  half_owners=$(gh issue view "$half_issue" --repo "$REPO" --json labels \
    --jq '[.labels[].name|select(startswith("agent:"))]|length' 2>/dev/null) || continue
  [ "$half_owners" = "0" ] || continue
  half_state=$(gh issue view "$half_issue" --repo "$REPO" --json labels \
    --jq '[.labels[].name|select(startswith("task:"))]|join(" ")' 2>/dev/null)
  # Same **From:** idiom gate.sh reads verdicts with, so one marker grammar
  # serves the whole package rather than a second one drifting here.
  # `unique`, then require exactly one — the same shape gate.sh's from_agent
  # uses, and the reason it is written that way. Taking .[0] would let a body
  # carrying two distinct markers hand the lane to whichever appeared first,
  # which is precisely the ownership guess this section exists to refuse.
  half_marker=$(printf '%s' "$half_detail" | jq -r '
    ([((.body // "") | split("\n")[]
       | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<id>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").id
       | ascii_downcase)] | unique) as $ids
    | if ($ids | length) == 1 then $ids[0] else "" end' 2>/dev/null)
  half_claims=$((half_claims + 1))
  printf '  #%s holds #%s, which still reads %s\n' \
    "$half_pr" "$half_issue" "${half_state:-no task label}"
  if [ -n "$half_marker" ]; then
    printf '      its owner re-runs claim.sh, or anyone repairs it:\n'
    printf '      gh pr edit %s --add-label agent:%s --add-label task:active\n' "$half_pr" "$half_marker"
    printf '      gh issue edit %s --add-label agent:%s --add-label task:active --remove-label task:ready\n' \
      "$half_issue" "$half_marker"
  else
    printf '      no **From:** marker on the PR — ask on the lane before labelling it\n'
  fi
done
[ "$half_claims" -eq 0 ] && echo "  none"

echo
echo "== holds that have been addressed — the author pushed after the label =="
# A hold transfers the obligation to whoever set it, and nothing else tells that
# person when it comes due. A review hold once remained for 75 minutes after the
# author had already fixed it because the reviewer never re-checked the PR.
#
# Every agent commits as the same handle, so the API cannot say WHICH agent set a
# label. All open holds are listed rather than filtered to yours: the cost of
# seeing someone else's is one glance, and the cost of hiding your own is the
# 75 minutes.
held=$(gh pr list --repo "$REPO" --state open --limit 60 \
         --json number,labels,title \
         --jq '.[]|select([.labels[].name]|any(startswith("hold:")))|"\(.number)\t\([.labels[].name]|map(select(startswith("hold:")))|join(","))\t\(.title[0:52])"' 2>/dev/null)

if [ -z "$held" ]; then
  echo "  none"
else
  printf '%s\n' "$held" | while IFS=$'\t' read -r n labels title; do
    # Latest application of any hold label; a hold can be set, cleared and set again.
    set_at=$(gh api "repos/$REPO/issues/$n/timeline" --paginate \
      --jq '[.[]|select(.event=="labeled" and (.label.name|startswith("hold:")))|.created_at]|last' 2>/dev/null)
    head_at=$(gh api "repos/$REPO/pulls/$n/commits" \
      --jq '[.[]|.commit.committer.date]|last' 2>/dev/null)
    echo "  #$n [$labels] $title"

    if [ -n "$set_at" ] && [ -n "$head_at" ]; then
      s_e=$(date -d "$set_at" +%s 2>/dev/null)
      h_e=$(date -d "$head_at" +%s 2>/dev/null)
      if [ -n "$s_e" ] && [ -n "$h_e" ] && [ "$h_e" -gt "$s_e" ]; then
        mins=$(( (h_e - s_e) / 60 ))
        waited=$(( ($(date +%s) - h_e) / 60 ))
        echo "        label set $set_at, head pushed +${mins}m later — WAITING ${waited}m. Re-review or clear."
      else
        echo "        no push since the label — the ball is with the author."
      fi
    fi
  done
fi

echo
echo "== reachability — self-reported at claim; where the owner was when written (#360) =="
echo "   (agents move between sessions; the board itself always reaches everyone)"
# The channel, not a session name: holds are clearable only by their owner, so
# reaching the owner is a correctness dependency of the hold mechanism, and
# the board is the one channel spanning every lineage, harness, and machine.
reach_prs=$(gh pr list --repo "$REPO" --state open --limit 40 \
  --json number,labels,body,updatedAt 2>/dev/null) || reach_prs='[]'
[ -n "$reach_prs" ] || reach_prs='[]'

# The channel, not a session name: holds are clearable only by their owner, so
# reaching the owner is a correctness dependency of the hold mechanism, and
# the board is the one channel spanning every lineage, harness, and machine.
# An agent that moves between sessions updates their reachability by appending
# a new **Reachable:** line rather than rewriting the body — so the LAST match
# is the current one (#390). `capture(...)` returns only the first; `match(;"g")`
# with `last` returns the last.
printf '%s' "$reach_prs" | jq -r '.[]
    | ([.labels[].name | select(startswith("agent:"))] | join(",")) as $agents
    | select($agents != "")
    | (((( .body // "") | [match("\\*\\*Reachable:\\*\\*\\s*(?<c>[^\\r\\n]+)"; "g") | .captures[0].string] | last)?) // "board (assumed — no roster line)") as $channel
    | "  #\(.number) [\($agents)] reachable: \($channel) · last seen \(.updatedAt)"' 2>/dev/null \
  || echo "  (gh unavailable)"

# The lane owner is not the person the steward usually needs (#373 review):
# on the motivating incident the lane said agent:fable while the unreachable
# agent was the HOLD owner, whom no label recorded at the time. channel
# lookup for whoever the holder turns out to be, from their own lane row
# above, else board.
agent_channels=$(printf '%s' "$reach_prs" | jq -r '.[]
    | ([.labels[].name | select(startswith("agent:")) | sub("^agent:"; "")] | .[]) as $a
    | (((( .body // "") | [match("\\*\\*Reachable:\\*\\*\\s*(?<c>[^\\r\\n]+)"; "g") | .captures[0].string] | last)?) // "") as $c
    | select($c != "")
    | "\($a)\t\($c)"' 2>/dev/null)

# Since #385, a review hold names its holder directly — hold:review:<agent> —
# so read that label rather than reconstructing ownership from the review
# stream at all. This is the primary path now; no heuristics involved.
printf '%s' "$reach_prs" | jq -r '.[]
    | . as $pr
    | ($pr.labels[].name | select(startswith("hold:review:")) | ltrimstr("hold:review:")) as $holder
    | "\($pr.number)\t\($holder)"' 2>/dev/null \
  | while IFS=$'\t' read -r held_pr holder; do
      [ -n "$held_pr" ] && [ -n "$holder" ] || continue
      channel=$(printf '%s\n' "$agent_channels" | awk -F'\t' -v a="$holder" '$1 == a {print $2; exit}')
      echo "  #$held_pr [hold] held by $holder — reachable: ${channel:-board (assumed — no lane of their own)}"
    done

# Legacy fallback only: a bare, unattributed `hold:review` label (pre-#385)
# names no holder, so reconstruct one the old way — the agent whose latest
# review verdict on that PR is changes-required — from the review stream
# **From:**/**Verdict:** markers gate.sh also parses. Migrate these PRs to
# hold.sh's named label and this block stops having anything to do.
printf '%s' "$reach_prs" | jq -r '.[]
    | select([.labels[].name] | any(. == "hold:review"))
    | .number' 2>/dev/null \
  | while read -r held_pr; do
      [ -n "$held_pr" ] || continue
      gh api "repos/$REPO/pulls/$held_pr/reviews" --paginate 2>/dev/null \
        | jq -s 'add // []' 2>/dev/null \
        | jq -r '
            [.[]
             | ((((.body // "") | split("\n") | .[0]
                 | capture("^\\*\\*From:\\*\\*\\s*(?<a>[a-z0-9]+([._-][a-z0-9]+)*)"; "i") | .a)?) // "") as $agent
             | select($agent != "")
             | (([(.body // "") | split("\n")[]
                 | (capture("^\\*\\*Verdict:\\*\\*\\s*(?<v>accept( with follow-up)?|approve|changes required|request changes)\\s*$"; "i") | .v)?
                 | select(. != null) | ascii_downcase
                 | if . == "approve" then "accept"
                   elif . == "request changes" then "changes required"
                   else . end] | last) // "") as $verdict
             | select($verdict != "")
             | {agent: $agent, verdict: $verdict, at: (.submitted_at // "")}]
            | group_by(.agent) | map(max_by(.at))
            | .[] | select(.verdict == "changes required") | .agent' 2>/dev/null \
        | while read -r holder; do
            [ -n "$holder" ] || continue
            channel=$(printf '%s\n' "$agent_channels" | awk -F'\t' -v a="$holder" '$1 == a {print $2; exit}')
            echo "  #$held_pr [hold, unattributed legacy label — migrate with hold.sh] held by $holder (reconstructed) — reachable: ${channel:-board (assumed — no lane of their own)}"
          done
    done

# The window between claim and PR: an agent-labelled issue with no open PR
# referencing it has an owner but no lane row — exactly when reaching the
# claimer matters most. Board-assumed; issues carry no roster line.
# mktemp + trap, not a predictable name: shared /tmp, early-exit leaks, and
# symlink clobbering are the classic trio (#373 review, non-blocking).
reach_bodies=$(mktemp)
trap 'rm -f "$reach_bodies"' EXIT
printf '%s' "$reach_prs" | jq -r '[.[] | .body // ""] | join("\n")' 2>/dev/null > "$reach_bodies"
gh issue list --repo "$REPO" --state open --limit 60 --json number,labels 2>/dev/null \
  | jq -r '.[]
      | ([.labels[].name | select(startswith("agent:"))] | join(",")) as $agents
      | select($agents != "")
      | "\(.number)\t\($agents)"' 2>/dev/null \
  | while IFS=$'\t' read -r inum iagents; do
      [ -n "$inum" ] || continue
      # Boundary-anchored: plain "#$inum" substring-matches, so issue #36
      # silently vanishes from this list whenever any PR body mentions #365 —
      # the same silent-omission class this board has fixed three times, and
      # under-reporting is the direction that costs (#373 review).
      grep -qE "#${inum}([^0-9]|\$)" "$reach_bodies" 2>/dev/null && continue
      echo "  #$inum [$iagents] (claimed, no PR yet) reachable: board (assumed)"
    done
rm -f "$reach_bodies"
trap - EXIT

echo
echo "== ready and unclaimed — no agent:* label =="
gh issue list --repo "$REPO" --state open --label "task:ready" --limit 40 \
  --json number,title,labels \
  --jq '.[]|select([.labels[].name]|any(startswith("agent:"))|not)|"  #\(.number) \(.title[0:70])"' 2>/dev/null \
  || echo "  (gh unavailable)"
# Unqueued issues — no task:* and no agent:* label — were invisible here for
# two missions because nothing produced task:ready, and the queue read as
# empty ("nothing to do") while work sat open (#366). Surface them in the
# same section; claim.sh accepts them directly.
gh issue list --repo "$REPO" --state open --limit 100 \
  --json number,title,labels \
  --jq '.[]|select(([.labels[].name] | any(startswith("agent:")) or any(startswith("task:")) or any(. == "ops:halt") | not))|"  #\(.number) (unqueued — no task label) \(.title[0:52])"' 2>/dev/null

echo
echo "== blocked =="
gh issue list --repo "$REPO" --state open --label "task:blocked" --limit 40 \
  --json number,title --jq '.[]|"  #\(.number) \(.title[0:70])"' 2>/dev/null

echo
echo "== open deliberations — decide.sh (#430) =="
# A product/architecture choice never reads as "waiting for owner" here: it is
# either still gathering votes (deadline not yet reached, or quorum-with-a-tie
# needing an explicit tie-break) or ready to close. Scanning active lanes only
# — the same open-PR / open-agent:*-issue set board.sh's hygiene pass already
# scans — keeps this bounded instead of a full-repository search.
deliberation_lanes=$( {
  gh pr list --repo "$REPO" --state open --limit 100 --json number --jq '.[].number' 2>/dev/null
  gh issue list --repo "$REPO" --state open --limit 100 --json number,labels \
    --jq '.[] | select([.labels[].name] | any(startswith("agent:"))) | .number' 2>/dev/null
} | sort -un )
deliberation_found=""
if [ -n "$deliberation_lanes" ]; then
  for lane in $deliberation_lanes; do
    lane_status=$(DECIDE_REPO="$REPO" "$SCRIPT_DIR/decide.sh" status "$lane" 2>/dev/null)
    if [ -n "$lane_status" ]; then
      printf '%s\n' "$lane_status"
      deliberation_found=1
    fi
  done
fi
[ -n "$deliberation_found" ] || echo "  (no open proposals on any active lane)"

echo
echo "== review-queue hygiene — unmergeable before review effort is spent (#366) =="
# A lane that bypassed claim.sh/ready.sh reaches task:review without the
# closing reference the gate requires; three PRs arrived unmergeable that way
# in one mission and nothing said so until merge time. Derive the same lane
# identity and call the same predicate as the gate so this warning is always
# clearable by ready.sh.
if review_prs=$(gh pr list --repo "$REPO" --state open --label "task:review" --limit 40 \
    --json number,title,body,headRefName 2>/dev/null) \
    && jq -e 'type == "array"' >/dev/null 2>&1 <<<"$review_prs"; then
  review_hygiene=""
  while IFS= read -r review_pr; do
    review_number=$(jq -r '.number' <<<"$review_pr")
    review_title=$(jq -r '.title // ""' <<<"$review_pr")
    review_branch=$(jq -r '.headRefName // ""' <<<"$review_pr")
    review_body=$(jq -r '.body // ""' <<<"$review_pr")
    review_issue=$(ai_team_derive_lane_issue "$review_title" "$review_branch" "$review_body" "")

    case "$review_issue" in
      error:*)
        review_hygiene+=$'\n'"  #$review_number has invalid lane identity — ${review_issue#error:}"
        ;;
      none)
        ;;
      *)
        if ! ai_team_body_has_closing_reference "$review_body" "$review_issue"; then
          review_hygiene+=$'\n'"  #$review_number has no closing reference to #$review_issue — run ready.sh before review effort is spent — ${review_title:0:48}"
        fi
        ;;
    esac
  done < <(jq -c '.[]' <<<"$review_prs")

  if [ -z "$review_hygiene" ]; then
    echo "  ok      every task:review lane satisfies the closing-reference contract"
  else
    printf '%s\n' "${review_hygiene#$'\n'}"
  fi
else
  echo "  (gh unavailable)"
fi

"$SCRIPT_DIR/label_hygiene.sh" "$REPO"

echo
BOARD_REPO="$REPO" "$(dirname "$0")/board.sh" hygiene
