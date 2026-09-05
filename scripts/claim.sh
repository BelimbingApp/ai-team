#!/usr/bin/env bash
#
# Claim one ready issue by creating its draft PR. This makes the board check a
# mechanism: no write occurs until both the issue and the open-PR registry say
# that the task is available.
#
#   CLAIM_AGENT=<stable-agent-id> docs/ai-team/scripts/claim.sh <issue-number>
#
# Optional: CLAIM_BRANCH=<branch>, CLAIM_TITLE=<PR title>, CLAIM_WORKTREE=<path>.
# The claim runs in a dedicated worktree so the shared root checkout stays on
# its current branch (normally main). Pass --head explicitly to gh so a
# multi-remote checkout cannot abort after the push.
#
# If a previous attempt pushed the claim branch but never opened the PR, a
# re-run resumes at the PR step instead of refusing the existing branch.

set -euo pipefail

here="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$here/_default_branch.sh"

issue="${1:-}"
agent="${CLAIM_AGENT:-}"

if [[ $# -ne 1 || ! "$issue" =~ ^[0-9]+$ ]]; then
  echo "usage: CLAIM_AGENT=<stable-agent-id> $0 <issue-number>" >&2
  exit 2
fi

if [[ ! "$agent" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
  echo "CLAIM_AGENT must be a lower-case stable agent id (without agent:)" >&2
  exit 2
fi

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "not a git checkout" >&2
  exit 2
}
cd "$root"

[[ -z "$(git status --porcelain)" ]] || {
  echo "refusing to claim with a dirty worktree" >&2
  exit 2
}

repo=$(ai_team_origin_repo) || {
  echo "cannot resolve the repository from origin" >&2
  exit 2
}
[[ -n "$repo" ]] || { echo "cannot resolve the repository from origin" >&2; exit 2; }

# A claim is the only onboarding mutation boundary. Establish its baseline
# here rather than relying on a prior session command: halt, branch identity,
# cleanliness, and remote currency are all live facts at claim time.
"$here/halt_status.sh" "$repo" >/dev/null || {
  status=$?
  echo "refusing to claim while halt status is active or unavailable" >&2
  exit "$status"
}
base_branch=$(ai_team_default_branch)

body=

claim_exit_cleanup() {
  local cleanup_status=$?
  trap - EXIT HUP INT TERM
  [[ -z "$body" ]] || rm -f -- "$body"
  exit "$cleanup_status"
}
trap claim_exit_cleanup EXIT
trap 'exit 130' HUP INT TERM

# Read the issue and every open PR before creating a branch, commit, or remote
# ref. GitHub does not offer a transaction across those resources; this is the
# closest useful boundary and every write below is fail-fast.
issue_json=$(gh issue view "$issue" --repo "$repo" --json state,labels,title,url 2>/dev/null) || {
  echo "cannot read issue #$issue from $repo" >&2
  exit 2
}

state=$(jq -r .state <<<"$issue_json")
if [[ "$state" != "OPEN" ]]; then
  echo "refusing #$issue: issue state is $state" >&2
  exit 1
fi

# An agent's own label is a resume, not a collision: filing a follow-up issue
# with your own label and then being refused by your own claim forced three
# manual claims in one mission, each silently skipping every invariant this
# script guarantees — Closes included — until the gate failed at merge time
# (#366). Anyone else's label is still a hard refusal, and an open claim PR
# is still caught by the registry check below either way.
holder=$(jq -r '[.labels[].name | select(startswith("agent:"))] | join(", ")' <<<"$issue_json")
own_label=0
if [[ "$holder" == "agent:$agent" ]]; then
  own_label=1
  echo "resuming #$issue: it already carries your own label ($holder)"
elif [[ -n "$holder" ]]; then
  echo "refusing #$issue: already held by $holder" >&2
  exit 1
fi

# Self-labelled follow-ups are typically filed without task:ready — the label
# was the claim of intent. An issue with NO task state at all is also
# claimable: absence of curation is not "not ready", and refusing it forced
# the other manual bypass (#366's second data set — an agent self-labelling
# to get past this very check). Only an explicit task state that is not
# ready — active, blocked, done — still refuses, by name.
ready=$(jq -r '[.labels[].name] | any(. == "task:ready")' <<<"$issue_json")
task_state=$(jq -r '[.labels[].name | select(startswith("task:"))] | join(", ")' <<<"$issue_json")
if [[ "$ready" != "true" && $own_label -eq 0 ]]; then
  if [[ -n "$task_state" ]]; then
    echo "refusing #$issue: its task state is $task_state, not task:ready" >&2
    exit 1
  fi
  echo "claiming unqueued #$issue: no task labels — the open-PR registry below is the collision guard"
fi

# The labels ARE the claim: gate.sh reads agent:<id> off the PR and orient.sh
# reads task:* off the issue. #15 happened because these three writes were an
# unchecked sequence — one of them not running left a lane nobody could see.
# Apply, then read back and say exactly what is missing. A failed *readback* is
# not the same as a missing label (gh may be unavailable, or stubbed): that
# warns and leaves the claim standing, while a successful readback showing an
# absent label is an error with the commands to finish it.
finish_claim_labels() {
  local pr="$1"
  local pr_labels="" issue_labels="" missing=""
  local pr_task_count=0 pr_task_conflict="" pr_label
  local issue_task_count=0 issue_task_conflict="" issue_label
  local -a pr_label_array=() issue_label_array=()

  gh pr edit "$pr" --repo "$repo" --add-label "agent:$agent" --add-label task:active || true
  gh issue edit "$issue" --repo "$repo" --add-label "agent:$agent" --add-label task:active || true
  # Tolerant removal: an own-label follow-up may never have carried task:ready.
  gh issue edit "$issue" --repo "$repo" --remove-label task:ready >/dev/null 2>&1 || true

  # The exit status, never the output, decides whether the readback happened.
  # An unlabelled resource answers with an EMPTY string and exits zero — and
  # that is precisely the half-claim this function exists to catch, so
  # treating empty as "could not read" would send the one case that matters
  # down the warning path and exit zero on it (#18 review, codex-gpt-5).
  local pr_read=1 issue_read=1 unread=""
  pr_labels=$(gh pr view "$pr" --repo "$repo" --json labels --jq '[.labels[].name] | join(",")' 2>/dev/null) || pr_read=0
  issue_labels=$(gh issue view "$issue" --repo "$repo" --json labels --jq '[.labels[].name] | join(",")' 2>/dev/null) || issue_read=0

  # Each side is judged on its own: one unreadable lookup must not hide a
  # missing label proven on the other.
  if [[ $pr_read -eq 1 ]]; then
    case ",$pr_labels," in *",agent:$agent,"*) ;; *) missing+="PR #$pr agent:$agent; " ;; esac
    IFS=',' read -r -a pr_label_array <<<"$pr_labels"
    for pr_label in "${pr_label_array[@]}"; do
      if [[ "$pr_label" == task:* ]]; then
        pr_task_count=$((pr_task_count + 1))
        [[ "$pr_label" == "task:active" ]] || pr_task_conflict="$pr_label"
      fi
    done
    if [[ $pr_task_count -ne 1 || -n "$pr_task_conflict" ]]; then
      missing+="PR #$pr task state must be exactly task:active (read: ${pr_labels:-none}); "
    fi
  else
    unread+="PR #$pr; "
  fi
  if [[ $issue_read -eq 1 ]]; then
    case ",$issue_labels," in *",agent:$agent,"*) ;; *) missing+="issue #$issue agent:$agent; " ;; esac
    IFS=',' read -r -a issue_label_array <<<"$issue_labels"
    for issue_label in "${issue_label_array[@]}"; do
      if [[ "$issue_label" == task:* ]]; then
        issue_task_count=$((issue_task_count + 1))
        [[ "$issue_label" == "task:active" ]] || issue_task_conflict="$issue_label"
      fi
    done
    if [[ $issue_task_count -ne 1 || -n "$issue_task_conflict" ]]; then
      missing+="issue #$issue task state must be exactly task:active (read: ${issue_labels:-none}); "
    fi
  else
    unread+="issue #$issue; "
  fi

  if [[ -z "$missing" ]]; then
    if [[ -n "$unread" ]]; then
      echo "warning: could not read back the labels for $unread" >&2
      echo "         The claim stands, but verify it: gh pr view $pr --json labels" >&2
    fi
    return 0
  fi

  echo "HALF-CLAIM: PR #$pr exists but the labels did not land — $missing" >&2
  # An unreadable side is reported here too. Naming only what was proven
  # missing would tell someone to fix the issue and stop, while the PR they
  # could not check stays unlabelled and the lane stays invisible.
  [[ -n "$unread" ]] && echo "warning: could not read back the labels for $unread" >&2
  echo "The board still reads #$issue as unclaimed, so another agent can collide." >&2
  echo "Finish it by re-running this script, or by hand:" >&2
  echo "  gh pr edit $pr --repo $repo --add-label agent:$agent --add-label task:active" >&2
  echo "  gh issue edit $issue --repo $repo --add-label agent:$agent --add-label task:active --remove-label task:ready" >&2
  return 1
}

prs=$(gh pr list --repo "$repo" --state open --limit 100 \
  --json number,title,body,headRefName,labels,url 2>/dev/null) || {
  echo "cannot read open pull requests from $repo" >&2
  exit 2
}

# A normal claim title is "... (#N)". Match that exact issue reference in the
# title or body. Also recognise this script's branch convention only when the
# PR has an owner label, so an unrelated branch cannot block the queue.
matches=$(jq -c --argjson issue "$issue" '
  def agent_labels: [.labels[].name | select(startswith("agent:"))];
  def issue_reference: "(#" + ($issue | tostring) + ")";
  def claim_branch:
    .headRefName | test("(^|[-_/])issue-?" + ($issue | tostring) + "($|[-_/])");
  def from_marker:
    ([((.body // "") | split("\n")[]
       | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<id>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").id
       | ascii_downcase)]) as $ids
    | if ($ids | length) > 0 then $ids[0] else "" end;
  [.[]
   | select(((((.title // "") + "\\n" + (.body // "")) | contains(issue_reference))
             or ((agent_labels | length) > 0 and claim_branch)))
   | {number, title, url, headRefName, holders: agent_labels, marker: from_marker}]
' <<<"$prs")

# A match carrying no agent:* label is a HALF-CLAIM: claim.sh created the PR
# and stopped before the labels landed (#15). That state is worse than no
# claim at all — the issue still reads task:ready, so the next agent claims it
# and collides, while the PR cannot pass gate.sh's "exactly one agent lane".
# The body's **From:** marker is the only identity that survived, and it
# decides what happens: your own half-claim is finished here, because re-running
# claim.sh is what an agent actually tries; anyone else's is still refused, but
# named, with the commands to repair it instead of a bare "already holds it".
mine=$(jq -c --arg agent "$agent" \
  '[.[] | select((.holders | length) == 0 and .marker == $agent)]' <<<"$matches")
blocking=$(jq -c --arg agent "$agent" \
  '[.[] | select((.holders | length) > 0 or .marker != $agent)]' <<<"$matches")
own_open=$(jq -c --arg agent "$agent" \
  '[.[] | select(.holders == ["agent:" + $agent] and .marker == $agent)]' <<<"$matches")
existing_pr=
existing_branch=

if [[ $(jq length <<<"$blocking") -eq 0 && $(jq length <<<"$mine") -eq 1 ]]; then
  repair_pr=$(jq -r '.[0].number' <<<"$mine")
  echo "repairing #$issue: PR #$repair_pr is your own half-claim — its labels never landed"
  finish_claim_labels "$repair_pr" || exit 1
  echo "repaired #$issue: PR #$repair_pr is agent:$agent / task:active"
  exit 0
fi

if [[ $own_label -eq 1 && $(jq length <<<"$matches") -eq 1 && $(jq length <<<"$own_open") -eq 1 ]]; then
  existing_pr=$(jq -r '.[0].number' <<<"$own_open")
  existing_branch=$(jq -r '.[0].headRefName' <<<"$own_open")
  echo "resuming #$issue: PR #$existing_pr is your own correctly labelled open claim"
elif [[ $(jq length <<<"$matches") -gt 0 ]]; then
  echo "refusing #$issue: an open PR already holds it:" >&2
  jq -r --arg agent "$agent" '.[]
    | if (.holders | length) == 0
      then "  #\(.number) [HALF-CLAIM by \(if .marker == "" then "an unnamed agent" else .marker end)] \(.title) — \(.url)"
      else "  #\(.number) [\(.holders | join(", "))] \(.title) — \(.url)" end' <<<"$matches" >&2
  if [[ $(jq '[.[] | select((.holders | length) == 0)] | length' <<<"$matches") -gt 0 ]]; then
    echo "" >&2
    echo "A HALF-CLAIM has a lane but no labels, so this issue still looks free to everyone else." >&2
    echo "Its owner finishes it by re-running claim.sh. Anyone may repair it by hand:" >&2
    jq -r --arg issue "$issue" '.[] | select((.holders | length) == 0)
      | "  gh pr edit \(.number) --add-label agent:\(if .marker == "" then "<owner>" else .marker end) --add-label task:active",
        "  gh issue edit \($issue) --add-label agent:\(if .marker == "" then "<owner>" else .marker end) --add-label task:active --remove-label task:ready"' <<<"$matches" >&2
  fi
  exit 1
fi

# Labels on live Issues and PRs are the identity registry. Create the lane label
# only after the claim has passed all availability checks, and before creating a
# branch or PR that would need it.
agent_label="agent:$agent"
labels=$(gh label list --repo "$repo" --limit 1000 --json name 2>/dev/null) || {
  echo "cannot read labels from $repo" >&2
  exit 2
}

# `label` is a jq keyword (label $out | break $out), so a variable named
# $label is a parse error on jq 1.6 — the existence check never ran and a
# second claim by the same agent aborted at `gh label create` (#403).
if ! jq -e --arg want "$agent_label" 'any(.name == $want)' <<<"$labels" >/dev/null; then
  gh label create "$agent_label" --repo "$repo" --color "5319e7" \
    --description "AI-team identity and ownership: $agent"
fi

branch="${CLAIM_BRANCH:-${existing_branch:-agent/${agent}-issue-${issue}}}"
if [[ -n "$existing_branch" && "$branch" != "$existing_branch" ]]; then
  echo "refusing #$issue: configured branch $branch does not match open PR #$existing_pr branch $existing_branch" >&2
  exit 1
fi
title="${CLAIM_TITLE:-$(jq -r .title <<<"$issue_json") (#${issue})}"
# Placing the lane worktree beside $root is only safe when $root's parent is
# inert. It is not when this repository is a nested checkout: for
# app/Extensions/SbGroup that resolves inside app/Extensions/, which the host
# application scans for modules, so a lane worktree can register phantom
# modules in the composed app (#445). Default outside the outermost work tree
# instead, and keep lanes together rather than scattering siblings.
if [[ -n "${CLAIM_WORKTREE:-}" ]]; then
  worktree="$CLAIM_WORKTREE"
else
  lane_root="${AI_TEAM_WORKTREE_ROOT:-}"
  if [[ -z "$lane_root" ]]; then
    # --show-superproject-working-tree only answers for a registered submodule.
    # It is EMPTY for an ordinary independent repository nested inside another
    # working tree, which is exactly the private-Extension shape this fix exists
    # for, so relying on it left lanes at <host>/app/Extensions/.ai-team-lanes —
    # still inside the scanner path. Walk outward instead until no enclosing
    # working tree remains.
    outermost="$root"
    probe=$(dirname "$outermost")
    while [[ -n "$probe" && "$probe" != "/" && "$probe" != "." ]]; do
      enclosing=$(git -C "$probe" rev-parse --show-toplevel 2>/dev/null || true)
      [[ -n "$enclosing" ]] || break
      outermost="$enclosing"
      probe=$(dirname "$enclosing")
    done
    lane_root="$(dirname "$outermost")/.ai-team-lanes"
  fi
  mkdir -p "$lane_root"
  # One worktree per agent per repository, recycled across lanes. A worktree
  # per issue multiplied checkouts of a large application until the disk was
  # the bottleneck; the lane is the branch, not the directory.
  worktree="$lane_root/$(basename "$root")-${agent}"
fi

local_branch=0
remote_branch=0
pushed_branch_sha=
fresh_local_sha=
worktree_recycled=0
git show-ref --verify --quiet "refs/heads/$branch" && local_branch=1
git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1 && remote_branch=1

resume=0
if [[ $local_branch -eq 1 || $remote_branch -eq 1 ]]; then
  # Branch without an open claim PR is a half-finished attempt — resume.
  resume=1
  echo "resuming #$issue: claim branch $branch already exists; opening the draft PR"
fi

rollback_partial_claim() {
  local current_local_tip worktree_status observed_lines observed
  # Undo only the fresh branch SHA created by this invocation. Never erase a
  # concurrently advanced branch or an existing resume branch.
  [[ $resume -eq 0 ]] || return 0

  current_local_tip=$(git rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)
  if [[ -z "$fresh_local_sha" || "$current_local_tip" != "$fresh_local_sha" ]]; then
    [[ -z "$current_local_tip" ]] || \
      echo "local claim branch changed to $current_local_tip during rollback; preserving its worktree and remote" >&2
    return 0
  fi

  if git worktree list --porcelain 2>/dev/null | grep -qx "worktree $worktree"; then
    worktree_status=$(git -C "$worktree" status --porcelain --untracked-files=normal 2>/dev/null) || {
      echo "cannot verify fresh claim worktree $worktree; preserving it and its refs" >&2
      return 0
    }
    if [[ -n "$worktree_status" ]]; then
      echo "fresh claim worktree $worktree changed during rollback; preserving it and its refs" >&2
      return 0
    fi
    if [[ $worktree_recycled -eq 1 ]]; then
      # The directory predates this claim: leave it, parked on the base tip.
      git -C "$worktree" switch -q --detach "origin/$base_branch" >/dev/null 2>&1 || {
        echo "cannot park recycled worktree $worktree; preserving its refs" >&2
        return 0
      }
    elif ! git worktree remove "$worktree" >/dev/null 2>&1; then
      echo "fresh claim worktree $worktree changed while rollback removed it; preserving its refs" >&2
      return 0
    fi
  elif [[ -d "$worktree" ]]; then
    echo "unregistered claim path $worktree exists; refusing recursive rollback and preserving refs" >&2
    return 0
  fi

  if [[ -n "$pushed_branch_sha" ]] && \
     ! git push --quiet --force-with-lease="refs/heads/$branch:$pushed_branch_sha" \
       origin ":refs/heads/$branch" >/dev/null 2>&1; then
    observed_lines=$(git ls-remote --heads origin "refs/heads/$branch" 2>/dev/null || true)
    observed=$(awk 'NF { print $1; exit }' <<<"$observed_lines")
    if [[ -n "$observed" ]]; then
      echo "remote claim branch is $observed after rollback refusal; preserving the exact local ref" >&2
      return 0
    fi
  fi
  if ! git update-ref -d "refs/heads/$branch" "$fresh_local_sha" >/dev/null 2>&1; then
    current_local_tip=$(git rev-parse --verify "refs/heads/$branch" 2>/dev/null || true)
    [[ -z "$current_local_tip" ]] || \
      echo "local claim branch changed to $current_local_tip during final rollback; preserving it" >&2
  fi
}

# Old claim.sh left the shared root on the claim branch after a failed
# gh pr create. Resume must free that checkout before attaching the branch
# to the lane worktree, then leave root on main.
restore_root_off_claim() {
  local current
  current=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  if [[ "$current" != "$branch" ]]; then
    return 0
  fi
  local base
  base=$(ai_team_default_branch)
  if git show-ref --verify --quiet "refs/heads/$base"; then
    git switch -q "$base"
  else
    git switch -q -c "$base" "origin/$base"
  fi
}

ensure_worktree() {
  # Always free the shared root first — the claim branch cannot be attached to
  # the lane worktree while root still has it checked out.
  restore_root_off_claim

  if [[ -d "$worktree" ]]; then
    # Old claim.sh often left a detached worktree at the claim tip. Accepting
    # the directory alone exits "success" while the author lane stays detached;
    # repair by attaching the claim branch without discarding local commits.
    (
      cd "$worktree"
      if [[ $local_branch -eq 1 ]]; then
        # Preserve any unpushed local tip — do not -C reset onto origin.
        git switch "$branch"
      elif [[ $remote_branch -eq 1 ]]; then
        git fetch -q origin "$branch"
        git switch -c "$branch" --track "origin/$branch" 2>/dev/null \
          || git switch -c "$branch" "origin/$branch"
      else
        echo "cannot attach worktree for missing branch $branch" >&2
        exit 2
      fi
    ) || return 1
    local_branch=1
    return 0
  fi

  if [[ $local_branch -eq 1 ]]; then
    # Prefer the local branch ref so the worktree is not detached.
    git worktree add "$worktree" "$branch"
  elif [[ $remote_branch -eq 1 ]]; then
    git fetch -q origin "$branch:refs/remotes/origin/$branch"
    git worktree add -b "$branch" "$worktree" "origin/$branch"
    local_branch=1
  else
    echo "cannot attach worktree for missing branch $branch" >&2
    exit 2
  fi
}

git fetch -q origin "$base_branch"

if [[ $resume -eq 0 ]]; then
  current_branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || {
    echo "refusing a new claim from detached HEAD" >&2
    exit 2
  }
  [[ "$current_branch" == "$base_branch" ]] || {
    echo "refusing a new claim from $current_branch; use the clean $base_branch checkout" >&2
    exit 1
  }
  [[ "$(git rev-parse HEAD)" == "$(git rev-parse "origin/$base_branch")" ]] || {
    echo "refusing a new claim: $base_branch must exactly match origin/$base_branch" >&2
    exit 1
  }
  restore_root_off_claim
  if git worktree list --porcelain 2>/dev/null | grep -qx "worktree $worktree"; then
    # Recycle the agent's worktree: it must be clean, and whatever it had
    # checked out must already be on origin (landed or pushed) so switching
    # away loses nothing.
    recycled_status=$(git -C "$worktree" status --porcelain --untracked-files=normal 2>/dev/null) || {
      echo "cannot read the state of $worktree; refusing to recycle it" >&2
      exit 2
    }
    if [[ -n "$recycled_status" ]]; then
      echo "refusing to claim: $worktree has uncommitted changes from a previous lane; commit, push, or discard them first" >&2
      exit 1
    fi
    if ! git -C "$worktree" branch -r --contains HEAD 2>/dev/null | grep -q .; then
      echo "refusing to claim: $worktree is on $(git -C "$worktree" rev-parse --short HEAD), which is on no remote branch; push or discard it first" >&2
      exit 1
    fi
    previous_branch=$(git -C "$worktree" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    git -C "$worktree" switch -q -c "$branch" "origin/$base_branch" || {
      echo "cannot switch $worktree to a new branch for #$issue" >&2
      exit 2
    }
    worktree_recycled=1
    if [[ -n "$previous_branch" ]] && git merge-base --is-ancestor "$previous_branch" "origin/$base_branch" 2>/dev/null; then
      git branch -q -D "$previous_branch" >/dev/null 2>&1 && echo "recycled $worktree; deleted landed branch $previous_branch"
    fi
  elif [[ -d "$worktree" ]]; then
    echo "refusing to claim: $worktree exists but is not a registered worktree of this checkout" >&2
    exit 1
  else
    git worktree add -b "$branch" "$worktree" "origin/$base_branch"
  fi
  fresh_local_sha=$(git -C "$worktree" rev-parse HEAD)
  git -C "$worktree" commit --allow-empty -m "claim: #$issue" || {
    echo "claim commit failed for #$issue — rolling back" >&2
    rollback_partial_claim
    exit 1
  }
  pushed_branch_sha=$(git -C "$worktree" rev-parse HEAD)
  fresh_local_sha=$pushed_branch_sha
  git -C "$worktree" push -u origin "$branch" || {
    echo "claim push failed for #$issue — rolling back" >&2
    rollback_partial_claim
    exit 1
  }
  remote_branch=1
  local_branch=1
else
  ensure_worktree
  # Ensure the remote tip exists for --head (local-only half claims).
  if [[ $remote_branch -eq 0 ]]; then
    (
      cd "$worktree"
      git push -u origin "$branch"
    ) || {
      echo "claim push failed while resuming #$issue — rolling back" >&2
      rollback_partial_claim
      exit 1
    }
    remote_branch=1
  fi
fi

if [[ -n "$existing_pr" ]]; then
  restore_root_off_claim
  echo "resumed #$issue: PR #$existing_pr on $branch"
  echo "worktree: $worktree"
  echo "root checkout left on $(git rev-parse --abbrev-ref HEAD)"
  exit 0
fi

body=$(mktemp)
# Closes #N must ship in the claim body: authors rewrite descriptions at handoff
# and forget the keyword, leaving merged PRs with open issues and a lying board.
# ready.sh re-asserts the same line when the PR leaves draft.
#
# **Reachable:** records the channel for reaching this lane's owner (#360) —
# a channel, never a session name: cross-lineage agents have no session
# address, and the agent that motivated the roster is one of them. The
# default is board, the only channel guaranteed to span every lineage,
# harness, and machine. Override with CLAIM_REACHABLE="session <name>" when
# a live session address exists; update by editing the PR body if it moves.
printf '**From:** %s\n\n**Reachable:** %s\n\nClaiming #%s through docs/ai-team/scripts/claim.sh.\n\nCloses #%s\n' \
  "$agent" "${CLAIM_REACHABLE:-board}" "$issue" "$issue" >"$body"

# --head is load-bearing on multi-remote checkouts: without it, gh cannot infer
# which remote owns the branch and aborts *after* the push, leaving an invisible
# half-claim. --base keeps the target explicit for the same reason.
if ! pr_url=$(gh pr create --repo "$repo" --draft --base "$base_branch" --head "$branch" \
  --title "$title" --body-file "$body"); then
  echo "gh pr create failed for #$issue" >&2
  if [[ $resume -eq 0 ]]; then
    echo "rolling back the orphan claim branch $branch" >&2
    rollback_partial_claim
  else
    echo "left existing branch $branch in place for another resume attempt" >&2
    echo "worktree: $worktree" >&2
  fi
  exit 1
fi

pr=${pr_url##*/}

# Never rolled back from here: the PR exists and may already carry work, so a
# labelling failure is reported loudly rather than undone silently.
if ! finish_claim_labels "$pr"; then
  echo "worktree: $worktree" >&2
  restore_root_off_claim
  exit 1
fi

restore_root_off_claim

echo "claimed #$issue in draft PR #$pr ($pr_url) as agent:$agent"
echo "worktree: $worktree"
echo "root checkout left on $(git rev-parse --abbrev-ref HEAD)"
