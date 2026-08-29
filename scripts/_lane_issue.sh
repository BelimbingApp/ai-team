#!/usr/bin/env bash
# Shared lane-issue identity and closing-reference contract for ready/gate/orient.
# Sourced — do not execute.
#
# Rules (fail closed on conflict):
# - Branch identity: ...issue-N... from claim.sh branch names.
# - Title identity: a trailing "(#N)" only (claim titles end that way); earlier
#   "(#N)" markers in the title are ignored so incidental numbers cannot win.
# - When both identities exist they must agree.
# - READY_ISSUE / override is allowed only when it agrees with every present
#   identity, or when neither title nor branch yields a number.
# - Issue-less path: neither title nor branch yields a number, and the PR body
#   contains an exact line `AI-Team-Lane-Issue: none`.

# Prints "N" or "none" or "error:<message>" on stdout.
ai_team_derive_lane_issue() {
  local title="${1:-}"
  local branch="${2:-}"
  local body="${3:-}"
  local override="${4:-}"

  local title_issue="" branch_issue=""
  local trimmed="$title"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"

  if [[ "$trimmed" =~ \(#([0-9]+)\)$ ]]; then
    title_issue="${BASH_REMATCH[1]}"
  fi
  if [[ "$branch" =~ (^|[-_/])issue-?([0-9]+)($|[-_/]) ]]; then
    branch_issue="${BASH_REMATCH[2]}"
  fi

  if [[ -n "$title_issue" && -n "$branch_issue" && "$title_issue" != "$branch_issue" ]]; then
    printf 'error:title (#%s) disagrees with branch issue-%s\n' "$title_issue" "$branch_issue"
    return 0
  fi

  local derived=""
  if [[ -n "$title_issue" ]]; then
    derived="$title_issue"
  elif [[ -n "$branch_issue" ]]; then
    derived="$branch_issue"
  fi

  if [[ -n "$override" ]]; then
    if [[ ! "$override" =~ ^[0-9]+$ ]]; then
      printf 'error:override must be a positive integer\n'
      return 0
    fi
    if [[ -n "$derived" && "$override" != "$derived" ]]; then
      printf 'error:READY_ISSUE #%s disagrees with derived #%s\n' "$override" "$derived"
      return 0
    fi
    printf '%s\n' "$override"
    return 0
  fi

  if [[ -n "$derived" ]]; then
    printf '%s\n' "$derived"
    return 0
  fi

  if printf '%s\n' "$body" | grep -qx 'AI-Team-Lane-Issue: none'; then
    printf 'none\n'
    return 0
  fi

  printf 'error:cannot derive issue from trailing (#N) or branch issue-N; pass READY_ISSUE or set AI-Team-Lane-Issue: none\n'
}

# Return success when the body carries a GitHub closing keyword bound to the
# supplied lane issue. GitHub treats the keywords case-insensitively; callers
# must derive the lane issue before asking this predicate.
ai_team_body_has_closing_reference() {
  local body="${1:-}"
  local issue="${2:-}"

  [[ "$issue" =~ ^[0-9]+$ ]] || return 2

  grep -qiE "(^|[^A-Za-z])(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]]+#${issue}([^0-9]|$)" <<<"$body"
}
