#!/usr/bin/env bash
# Project-local orientation. This is the one file an adopting repository
# replaces: keep repository-specific facts — source pins, assembly checks,
# project commands — here, so the guide and the generic mechanisms beside it
# stay portable.
#
# This copy is the home repository's own, where the project *is* the package.

PROJECT_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
# shellcheck source=scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$PROJECT_DIR/_default_branch.sh"
BASE=$(ai_team_default_branch)

set -u

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
git ls-tree --name-only "origin/$BASE" -- scripts 2>/dev/null | wc -l \
  | awk '{print "  scripts/  " $1 " tracked file(s)"}'
git ls-tree --name-only "origin/$BASE" -- scripts 2>/dev/null | grep -c '^scripts/test_' \
  | awk '{print "  tests     " $1 " test module(s)"}'

cat <<'TXT'

== ai-team project: commands worth knowing ==
  python3 -m unittest discover -s scripts -p 'test_*.py'   run the mechanism suite
  scripts/orient.sh                                        read the board
  scripts/gate.sh <pr> <full-sha>                          gate a merge

Every change here lands in the repositories that mount this package at
docs/ai-team/, so a mechanism change is a change to how other teams work.
Prove it with a test in the same PR.
TXT
