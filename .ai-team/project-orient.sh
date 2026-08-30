#!/usr/bin/env bash
# Project-local orientation. This is the one file an adopting repository
# replaces: keep repository-specific facts — source pins, assembly checks,
# project commands — here, so the guide and the generic mechanisms beside it
# stay portable.
#
# This copy is the home repository's own, where the project *is* the package.

# orient.sh exports AI_TEAM_DEFAULT_BRANCH before invoking this hook — copied
# out to .ai-team/ at the repository root (#8), this file has no relative path
# back to scripts/_default_branch.sh, so it cannot resolve the branch itself.
# The fallback covers a direct run of this hook outside orient.sh.
set -u
BASE="${AI_TEAM_DEFAULT_BRANCH:-$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')}"
BASE="${BASE:-main}"

echo "== ai-team project: toolchain the mechanisms need =="
for tool in bash python3 git gh jq; do
  if command -v "$tool" >/dev/null 2>&1; then
    case "$tool" in
      bash)    version=$(bash --version | head -1) ;;
      python3) version=$(python3 --version) ;;
      git)     version=$(git --version) ;;
      gh)      version=$(gh --version | head -1) ;;
      jq)      version=$(jq --version) ;;
    esac
    echo "  ok      $version"
  else
    echo "  MISSING $tool is not on PATH — mechanisms that call it will fail"
  fi
done
if command -v shellcheck >/dev/null 2>&1; then
  echo "  ok      $(shellcheck --version | awk '/^version:/ {print "shellcheck " $2}')"
else
  echo "  note    shellcheck is absent; CI still lints the scripts"
fi

echo
echo "== ai-team project: what ships from origin/$BASE =="
# -r, or ls-tree reports the `scripts` tree entry itself rather than its
# contents: the first count is then always 1 and the second always 0, which is
# what this hook printed to every agent that ran orient.sh (#10).
shipped=$(git ls-tree -r --name-only "origin/$BASE" -- scripts 2>/dev/null)
printf '  scripts/  %s tracked file(s)\n' "$(printf '%s\n' "$shipped" | grep -c '^scripts/')"
printf '  tests     %s test module(s)\n' "$(printf '%s\n' "$shipped" | grep -c '^scripts/test_')"

cat <<'TXT'

== ai-team project: commands worth knowing ==
  python3 -m unittest discover -s scripts -p 'test_*.py'   run the mechanism suite
  scripts/orient.sh                                        read the board
  scripts/gate.sh <pr> <full-sha>                          gate a merge

Every change here lands in the repositories that mount this package at
docs/ai-team/, so a mechanism change is a change to how other teams work.
Prove it with a test in the same PR.
TXT
