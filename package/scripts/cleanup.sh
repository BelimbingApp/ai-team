#!/usr/bin/env bash
#
# cleanup.sh — leave the checkout clean when you stop.
#
#   docs/ai-team/scripts/cleanup.sh          dry run: shows what it would remove
#   docs/ai-team/scripts/cleanup.sh --yes    delete merged branches, prune worktrees
#
# A task is done when nothing you created is left lying around, and untidiness is
# invisible to whoever made it. This removes only what is provably finished:
# local branches fully merged into the default branch, and stale worktree refs. It
# never touches an unmerged branch or an active worktree. Background loops and
# heartbeats it only *lists* — a shell cannot cancel your tool's scheduler; stop
# those where you started them (your heartbeat cron, your watcher). Remote branch
# deletion remains explicit because a shared checkout cannot infer which remote
# branches belong to the caller.
#
set -u

CLEANUP_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$CLEANUP_DIR/_default_branch.sh"
BASE=$(ai_team_default_branch)

apply=0
[ "${1:-}" = "--yes" ] && apply=1

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git checkout" >&2; exit 2; }
cd "$ROOT" || exit 2

if ! git fetch -q origin "$BASE" 2>/dev/null; then
  echo "cannot refresh origin/$BASE; cleanup stopped without deleting anything" >&2
  exit 2
fi

echo "== merged local branches (every commit already in origin/$BASE) =="
current=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)   # "HEAD" when detached
any=0
while IFS= read -r b; do
  [ -z "$b" ] && continue
  [ "$b" = "$BASE" ] && continue
  git merge-base --is-ancestor "$b" "origin/$BASE" 2>/dev/null || continue
  any=1
  if [ "$b" = "$current" ]; then
    echo "  $b — current branch; detach (git checkout --detach origin/$BASE) then re-run"
  elif [ "$apply" -eq 1 ]; then
    if git branch -D "$b" >/dev/null 2>&1; then
      echo "  deleted $b"
    else
      echo "  kept $b (checked out in another worktree — remove that worktree first)"
    fi
  else
    echo "  would delete $b"
  fi
done < <(git for-each-ref --format='%(refname:short)' refs/heads/)
[ "$any" -eq 0 ] && echo "  none"

echo
echo "== unmerged local branches (left alone — verify before deleting by hand) =="
any=0
while IFS= read -r b; do
  [ -z "$b" ] || [ "$b" = "$BASE" ] && continue
  git merge-base --is-ancestor "$b" "origin/$BASE" 2>/dev/null && continue
  echo "  $b"
  any=1
done < <(git for-each-ref --format='%(refname:short)' refs/heads/)
[ "$any" -eq 0 ] && echo "  none"

echo
echo "== worktrees =="
git worktree list 2>/dev/null | sed 's/^/  /'
if [ "$apply" -eq 1 ]; then
  pruned=$(git worktree prune -v 2>&1)
  [ -n "$pruned" ] && printf '%s\n' "$pruned" | sed 's/^/  pruned: /' || echo "  nothing stale to prune"
  echo "  note: your own active worktree cannot self-remove; run 'git worktree remove <path>'"
  echo "        from another checkout once this session ends."
else
  echo "  (--yes runs 'git worktree prune' — removes only stale refs, never an active worktree)"
fi

echo
echo "== loops still running under you — cancel these in your tool =="
# A shell cannot cancel a heartbeat cron or a background watcher it did not
# start in-process; it can only surface them so you stop them where you did.
loops=$(ps -eo pid,args 2>/dev/null \
  | grep -E 'gate\.sh [0-9]+|pulls/[0-9]+/merge|for i in .*\$\(seq|gh pr (view|checks) [0-9]+' \
  | grep -vE 'grep|cleanup\.sh')
if [ -z "$loops" ]; then
  echo "  none detected (heuristic — also cancel any heartbeat/scheduled wakeup you set)"
else
  printf '%s\n' "$loops" | sed 's/^/  /'
  echo "  ^ stop these where you started them, and cancel your heartbeat/scheduled wakeup."
fi
