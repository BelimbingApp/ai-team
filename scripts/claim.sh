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

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) || {
  echo "cannot resolve the repository from gh" >&2
  exit 2
}

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
  [.[]
   | select(((((.title // "") + "\\n" + (.body // "")) | contains(issue_reference))
             or ((agent_labels | length) > 0 and claim_branch)))
   | {number, title, url, holders: agent_labels}]
' <<<"$prs")

if [[ $(jq length <<<"$matches") -gt 0 ]]; then
  echo "refusing #$issue: an open PR already holds it:" >&2
  jq -r '.[] | "  #\(.number) [\(.holders | join(", "))] \(.title) — \(.url)"' <<<"$matches" >&2
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

branch="${CLAIM_BRANCH:-agent/${agent}-issue-${issue}}"
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
  worktree="$lane_root/$(basename "$root")-${agent}-issue-${issue}"
fi

local_branch=0
remote_branch=0
git show-ref --verify --quiet "refs/heads/$branch" && local_branch=1
git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1 && remote_branch=1

resume=0
if [[ $local_branch -eq 1 || $remote_branch -eq 1 ]]; then
  # Branch without an open claim PR is a half-finished attempt — resume.
  resume=1
  echo "resuming #$issue: claim branch $branch already exists; opening the draft PR"
fi

rollback_partial_claim() {
  # Best-effort undo after a post-push failure so the board stays empty.
  git push origin --delete "$branch" >/dev/null 2>&1 || true
  if git worktree list --porcelain 2>/dev/null | grep -qx "worktree $worktree"; then
    git worktree remove --force "$worktree" >/dev/null 2>&1 || true
  elif [[ -d "$worktree" ]]; then
    rm -rf "$worktree"
    git worktree prune >/dev/null 2>&1 || true
  fi
  git branch -D "$branch" >/dev/null 2>&1 || true
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
    git worktree add -b "$branch" "$worktree" "origin/$branch"
    local_branch=1
  else
    echo "cannot attach worktree for missing branch $branch" >&2
    exit 2
  fi
}

base_branch=$(ai_team_default_branch)
git fetch -q origin "$base_branch"

if [[ $resume -eq 0 ]]; then
  restore_root_off_claim
  git worktree add -b "$branch" "$worktree" "origin/$base_branch"
  (
    cd "$worktree"
    git commit --allow-empty -m "claim: #$issue"
    git push -u origin "$branch"
  ) || {
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

body=$(mktemp)
trap 'rm -f "$body"' EXIT
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

gh pr edit "$pr" --repo "$repo" --add-label "agent:$agent" --add-label task:active
# Tolerant removal: an own-label follow-up may never have carried task:ready,
# and failing here would abort after the PR already exists.
gh issue edit "$issue" --repo "$repo" --add-label "agent:$agent" --add-label task:active
gh issue edit "$issue" --repo "$repo" --remove-label task:ready 2>/dev/null || true

restore_root_off_claim

echo "claimed #$issue in draft PR #$pr ($pr_url) as agent:$agent"
echo "worktree: $worktree"
echo "root checkout left on $(git rev-parse --abbrev-ref HEAD)"
