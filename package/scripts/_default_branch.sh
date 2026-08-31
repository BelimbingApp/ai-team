#!/usr/bin/env bash
# Shared default-branch resolution for the ai-team mechanisms.
# Sourced — do not execute.
#
# Why this exists: `main` was hardcoded across claim.sh, gate.sh, orient.sh and
# cleanup.sh, so copying this package into a repository whose default branch is
# `master` broke the first command an onboarding agent runs and left gate.sh
# proving containment against a ref that does not exist (#445).
#
# Why it resolves the way it does — both obvious sources were observed lying in
# the same real checkout that motivated this:
#
#   `gh repo view` answered for a DIFFERENT repository. A checkout carrying
#   `remote.<name>.gh-resolved = base` makes bare `gh` resolve to that repo from
#   anywhere in the tree, and it outranks GH_REPO. So gh's ambient answer is not
#   about origin, and the base branch must be a branch on origin: claim.sh builds
#   the lane worktree from `origin/<base>` and gate.sh proves containment against
#   it. We therefore ask gh about the repository origin actually points at.
#
#   `refs/remotes/origin/HEAD` was STALE — it named a long-dead branch while the
#   repository's default had moved. It is still worth consulting, because it
#   needs no network and is usually right, but it cannot come first.
#
# Order: explicit override, then origin's real repository per gh, then
# origin/HEAD, then whichever of main/master exists on origin, then `main` so a
# fresh checkout behaves exactly as it did before this change.
#
# Cached per process: at most one network round trip per invocation.

# Prints "OWNER/REPO" for the origin remote, or nothing.
ai_team_origin_repo() {
  local url
  url=$(git remote get-url origin 2>/dev/null) || return 1
  url="${url%.git}"
  case "$url" in
    *github.com[:/]*) printf '%s' "${url##*github.com}" | sed -e 's|^[:/]||' ;;
    *) return 1 ;;
  esac
}

ai_team_default_branch() {
  if [ -n "${_AI_TEAM_BASE_BRANCH_CACHE:-}" ]; then
    printf '%s' "$_AI_TEAM_BASE_BRANCH_CACHE"
    return 0
  fi

  local candidates=() origin_repo="" reported="" head_ref="" candidate="" chosen=""

  # An explicit override is honoured verbatim and never verified. It exists so a
  # repository can gate against something the resolver cannot infer, and silently
  # substituting an inferred branch for the one an operator named would be the
  # worst outcome available: they asked for X, got Y, and were not told. If the
  # branch is wrong the next git command says so, loudly and with the real name.
  if [ -n "${AI_TEAM_BASE_BRANCH:-}" ]; then
    _AI_TEAM_BASE_BRANCH_CACHE="$AI_TEAM_BASE_BRANCH"
    printf '%s' "$_AI_TEAM_BASE_BRANCH_CACHE"
    return 0
  fi

  if origin_repo=$(ai_team_origin_repo) && [ -n "$origin_repo" ]; then
    reported=$(gh repo view "$origin_repo" --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null) || reported=""
    [ "$reported" = "null" ] && reported=""
    [ -n "$reported" ] && candidates+=("$reported")
  fi

  head_ref=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null) || head_ref=""
  head_ref="${head_ref#origin/}"
  [ -n "$head_ref" ] && candidates+=("$head_ref")

  candidates+=(main master)

  # A candidate only counts if origin actually has that branch. This rejects a
  # stale refs/remotes/origin/HEAD naming a deleted branch, and anything a
  # misconfigured or stubbed `gh` reports that is not a branch at all.
  for candidate in "${candidates[@]}"; do
    [ -n "$candidate" ] || continue
    if git show-ref --verify --quiet "refs/remotes/origin/$candidate" 2>/dev/null; then
      chosen="$candidate"
      break
    fi
    if git ls-remote --exit-code --heads origin "$candidate" >/dev/null 2>&1; then
      chosen="$candidate"
      break
    fi
  done

  # Nothing verifiable — a fresh checkout with no remote refs, or offline. Take
  # the first stated preference so behaviour is unchanged from before this
  # resolver existed.
  if [ -z "$chosen" ]; then
    for candidate in "${candidates[@]}"; do
      [ -n "$candidate" ] && { chosen="$candidate"; break; }
    done
  fi
  [ -n "$chosen" ] || chosen="main"

  _AI_TEAM_BASE_BRANCH_CACHE="$chosen"
  printf '%s' "$chosen"
}
