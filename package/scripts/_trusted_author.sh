#!/usr/bin/env bash
# Shared, fail-closed mapping from GitHub's immutable REST pull-request identity
# to an automated AI-team author lane. Sourced — do not execute.
#
# Human lanes prove authorship through exactly one `agent:<id>` label placed by
# claim.sh. Trusted service-owned pull requests cannot use that claim protocol,
# so the narrow exception lives here and is consumed by review_gate.sh,
# gate.sh, and land.sh alike. Branch names, titles, and ordinary labels are not
# evidence: a contributor can choose all three.

# Print the stable automated-author lane, or nothing when the pull request is not
# from an exact trusted GitHub identity. Dependabot's numeric account id is the
# trust anchor; login and type are corroboration. Requiring equal numeric head
# and base repository ids excludes fork-authored PRs. Always returns success so
# callers can use the empty result as the ordinary-human path under `set -e`.
ai_team_trusted_automated_author_lane() {
  local pull_json="${1:-}"

  [[ -n "$pull_json" ]] || pull_json='{}'

  if jq -e '
    .user.id == 49699333
    and .user.login == "dependabot[bot]"
    and .user.type == "Bot"
    and (.head.repo.id | type) == "number"
    and (.base.repo.id | type) == "number"
    and .head.repo.id == .base.repo.id
  ' >/dev/null 2>&1 <<<"$pull_json"; then
    printf 'github-dependabot\n'
  fi
}
