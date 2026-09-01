#!/usr/bin/env bash
#
# Decide whether a pull request is a pure, verifiable subtree pull of this
# package — the trusted SHAPE that needs no adopter-side agent review (#61),
# symmetric with the trusted-author lane (#52): review happened once, at the
# source, behind this repository's own gate; the adopter only verifies.
#
#   subtree_pull_gate.sh <base-sha> <head-sha> <mount-prefix> <upstream-repo> <upstream-branch>
#
# Exit 0: the trusted shape holds — every commit touching <mount-prefix> is a
#   git-subtree squash or its merge (with the fail-closed and refresh-merge
#   semantics belimbing's mount guard learned on first contact), the resulting
#   mount tree is byte-identical to a commit reachable on the upstream branch,
#   and every change outside the mount is a workflow regenerated from a pulled
#   template (identical after the leading comment block).
# Exit 1: not the trusted shape — the ordinary independent-review requirement
#   applies. This is not a refusal of the PR, only of the exemption.
# Exit 2: this environment cannot judge (missing objects, failed fetch,
#   unreadable input). Callers must treat 2 as "review required", never as
#   "trusted": the exemption fails closed.
#
# Callers run this from a TRUSTED checkout (the base repository). PR-side
# commits are read as git objects only — nothing from the pull request is ever
# executed here.
set -uo pipefail

base="${1:-}"
head="${2:-}"
prefix="${3:-}"
upstream_repo="${4:-}"
upstream_branch="${5:-}"

if [[ -z "$base" || -z "$head" || -z "$prefix" || -z "$upstream_repo" || -z "$upstream_branch" ]]; then
  echo "usage: subtree_pull_gate.sh <base-sha> <head-sha> <mount-prefix> <upstream-repo> <upstream-branch>" >&2
  exit 2
fi
if [[ ! "$upstream_repo" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "subtree-pull-gate: upstream repository must be an owner/name slug" >&2
  exit 2
fi
prefix="${prefix%/}"

squash_prefix="Squashed '$prefix/' changes from "

say_no()  { echo "subtree-pull-gate: not a trusted pull shape: $*" >&2; exit 1; }
say_err() { echo "subtree-pull-gate: cannot judge: $*" >&2; exit 2; }

for endpoint in "$base" "$head"; do
  git cat-file -e "$endpoint^{commit}" 2>/dev/null \
    || say_err "endpoint $endpoint is not present in this checkout"
done

# Exit 0 = no difference, 1 = difference, >1 = error (missing objects). The
# exemption must never be granted on an error path.
mount_diff() {
  local status
  git diff --quiet "$1" "$2" -- "$prefix/" 2>/dev/null
  status=$?
  [[ "$status" -le 1 ]] || say_err "git diff $1..$2 failed for $prefix/"
  return "$status"
}

is_squash_commit() {
  [[ "$(git log -1 --format=%s "$1" 2>/dev/null)" == "$squash_prefix"* ]]
}

is_pull_merge_commit() {
  local parents second_parent
  parents=$(git log -1 --format=%P "$1")
  [[ "$(wc -w <<<"$parents")" -ge 2 ]] || return 1
  second_parent=$(awk '{print $2}' <<<"$parents")
  is_squash_commit "$second_parent"
}

merge_introduces_mount_change() {
  local parent
  for parent in $(git log -1 --format=%P "$1"); do
    if mount_diff "$parent" "$1"; then
      return 1
    fi
  done
  return 0
}

range_commits=$(git rev-list "$base..$head") \
  || say_err "git rev-list $base..$head failed"

# 1. Every commit touching the mount is subtree-pull-shaped.
touched_mount=0
while IFS= read -r commit; do
  [[ -n "$commit" ]] || continue
  mount_diff "$commit^" "$commit" && continue
  touched_mount=1
  is_squash_commit "$commit" && continue
  is_pull_merge_commit "$commit" && continue
  if [[ "$(git log -1 --format=%P "$commit" | wc -w)" -ge 2 ]] \
    && ! merge_introduces_mount_change "$commit"; then
    continue
  fi
  say_no "commit $commit ($(git log -1 --format=%s "$commit")) edits $prefix/ outside the subtree-pull shape"
done <<<"$range_commits"

[[ "$touched_mount" == "1" ]] \
  || say_no "the range never touches $prefix/ — the exemption is only for pulls"

# 2. The resulting mount tree exists on the upstream branch. The fetch pins
#    the branch into a local ref so the comparison set is exactly what the
#    upstream serves at judgment time; a moved branch between fetch and
#    compare cannot widen it.
# AI_TEAM_TEST_UPSTREAM_URL is a hermetic-test seam only (file:// fixture
# remotes); production callers never set it and always fetch canonical GitHub.
upstream_url="${AI_TEAM_TEST_UPSTREAM_URL:-https://github.com/$upstream_repo.git}"
git fetch -q --no-tags "$upstream_url" \
  "+refs/heads/$upstream_branch:refs/subtree-pull-gate/upstream" 2>/dev/null \
  || say_err "cannot fetch $upstream_repo $upstream_branch"

mount_tree=$(git rev-parse "$head:$prefix" 2>/dev/null) \
  || say_err "cannot resolve $prefix/ tree at $head"

# Membership is not enough: every historical upstream tree stays reachable
# forever, so "matches some upstream commit" would exempt a DOWNGRADE — and a
# downgraded mount carries downgraded templates, letting one unreviewed PR
# reinstall a superseded review workflow (claude-opus-5's blocker on #61).
# The pull must move FORWARD: the upstream commit matching the base mount must
# be an ancestor of the one matching the head mount. A base mount that exists
# nowhere upstream is an adopter deviation, and an absent base mount is an
# initial add — both get an ordinary review.
base_tree=$(git rev-parse "$base:$prefix" 2>/dev/null) \
  || say_no "the base has no $prefix/ mount — an initial mount needs an ordinary review"

head_commit=""
base_commit=""
while IFS= read -r upstream_commit; do
  upstream_tree=$(git rev-parse "$upstream_commit^{tree}")
  [[ -z "$head_commit" && "$upstream_tree" == "$mount_tree" ]] && head_commit="$upstream_commit"
  [[ -z "$base_commit" && "$upstream_tree" == "$base_tree" ]] && base_commit="$upstream_commit"
  [[ -n "$head_commit" && -n "$base_commit" ]] && break
done < <(git rev-list refs/subtree-pull-gate/upstream)

[[ -n "$head_commit" ]] \
  || say_no "the pulled $prefix/ tree matches no commit on $upstream_repo@$upstream_branch"
[[ -n "$base_commit" ]] \
  || say_no "the base $prefix/ tree matches no commit on $upstream_repo@$upstream_branch — adopter deviation needs an ordinary review"
git merge-base --is-ancestor "$base_commit" "$head_commit" 2>/dev/null \
  || say_no "the pull moves $prefix/ backward: upstream $base_commit is not an ancestor of $head_commit"

# 3. Every change outside the mount is a workflow file regenerated from a
#    pulled template: identical after each side's leading comment block, so an
#    adopter header comment is free but any behavioral deviation ends the
#    exemption. Everything else outside the mount requires a review.
strip_leading_comments() {
  awk 'started || !/^([[:space:]]*#.*)?$/ { started = 1; print }' "$1"
}

outside=$(git diff --name-only "$base" "$head" -- . ":(exclude)$prefix/" 2>/dev/null) \
  || say_err "git diff for non-mount files failed"

workdir=$(mktemp -d) || say_err "cannot allocate a scratch directory"
trap 'rm -rf "$workdir"' EXIT

while IFS= read -r file; do
  [[ -n "$file" ]] || continue
  case "$file" in
    .github/workflows/*.yml|.github/workflows/*.yaml) ;;
    *) say_no "non-mount change to $file is outside the template-workflow allowance" ;;
  esac
  template="$prefix/templates/$(basename "$file" | sed 's/^ai-team-//')"
  if ! git cat-file -e "$head:$template" 2>/dev/null; then
    say_no "$file has no counterpart template at $template in the pulled mount"
  fi
  git show "$head:$file" >"$workdir/installed" 2>/dev/null \
    || say_err "cannot read $file at $head"
  git show "$head:$template" >"$workdir/template" 2>/dev/null \
    || say_err "cannot read $template at $head"
  if ! diff -q <(strip_leading_comments "$workdir/installed") \
              <(strip_leading_comments "$workdir/template") >/dev/null; then
    say_no "$file deviates from its pulled template beyond the leading comment block"
  fi
done <<<"$outside"

echo "subtree-pull-gate: trusted pull shape verified for $prefix/ from $upstream_repo@$upstream_branch"
exit 0
