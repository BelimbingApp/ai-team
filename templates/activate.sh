#!/usr/bin/env bash
# Activation boundary for an adopting repository. Copy this file and
# package-refresh.conf to .ai-team/, then run .ai-team/activate.sh instead of
# orient.sh when a team session starts. An update is built and tested away from
# the caller's checkout; onboarding resumes only after its PR has merged.
set -u

PREFIX=docs/ai-team
UPDATE_BRANCH=ai-team/package-refresh
MUTEX_BRANCH=ai-team/activation-mutex
BOOTSTRAP_AGENT=package-bootstrap
CONFIG=.ai-team/package-refresh.conf
MUTEX_REF="refs/heads/$MUTEX_BRANCH"
MUTEX_SHA=
MUTEX_HELD=0
MUTEX_EMPTY_RETRY_COUNT=0
TEMP_PARENT=
TEMP_WORKTREE=
TEMP_SUITE_REPO=
BODY_FILE=
WORKTREE_ADDED=0
PUBLISHED_CLAIM_SHA=
PUBLISHED_CLAIM_ACTIVE=0
REMOTE_UPDATE_SHA=

fail() {
  printf 'activation: %s\n' "$*" >&2
  exit 1
}

# Repository-selection environment can redirect even `git -C` and `git init`
# into adopter-owned Git state. Activation never guesses through it: enter the
# intended checkout normally, with all selectors unset.
[ -z "${GIT_DIR:-}" ] || fail "GIT_DIR must be unset before activation"
[ -z "${GIT_WORK_TREE:-}" ] || fail "GIT_WORK_TREE must be unset before activation"
[ -z "${GIT_COMMON_DIR:-}" ] || fail "GIT_COMMON_DIR must be unset before activation"
[ -z "${GIT_OBJECT_DIRECTORY:-}" ] || fail "GIT_OBJECT_DIRECTORY must be unset before activation"
[ -z "${GIT_ALTERNATE_OBJECT_DIRECTORIES:-}" ] || \
  fail "GIT_ALTERNATE_OBJECT_DIRECTORIES must be unset before activation"
[ -z "${GIT_INDEX_FILE:-}" ] || fail "GIT_INDEX_FILE must be unset before activation"
[ -z "${GIT_NAMESPACE:-}" ] || fail "GIT_NAMESPACE must be unset before activation"
[ -z "${GIT_CONFIG_PARAMETERS:-}" ] || fail "GIT_CONFIG_PARAMETERS must be unset before activation"
[ -z "${GIT_CONFIG_COUNT:-}" ] || fail "GIT_CONFIG_COUNT must be unset before activation"

stop_for_pr() {
  printf 'activation: %s\n' "$*" >&2
  printf 'activation: onboarding is paused until the package refresh PR is merged and the default branch is updated locally\n' >&2
  exit 3
}

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || fail "not a git checkout"
cd "$ROOT" || fail "cannot enter repository root"

acquire_activation_mutex() {
  mutex_nonce=$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n') || {
    printf 'activation: cannot generate a unique activation mutex claim\n' >&2
    return 2
  }
  printf '%s\n' "$mutex_nonce" | grep -Eq '^[0-9a-fA-F]{32}$' || {
    printf 'activation: cannot generate a valid activation mutex claim\n' >&2
    return 2
  }
  mutex_tree=$(git rev-parse "$base_sha^{tree}" 2>/dev/null) || {
    printf 'activation: cannot inspect the default tree for the activation mutex\n' >&2
    return 2
  }
  mutex_message=$(printf 'AI Team activation/claim mutex\n\nAI-Team-Activation-Mutex: true\nAI-Team-Activation-Mutex-Base: %s\nAI-Team-Activation-Mutex-Owner: package-refresh:%s\nAI-Team-Activation-Mutex-Nonce: %s\n' \
    "$base_sha" "$UPSTREAM_SHA" "$mutex_nonce")
  MUTEX_SHA=$(git -c user.name=ai-team-activation-mutex \
    -c user.email=ai-team-activation-mutex@users.noreply.github.com \
    commit-tree "$mutex_tree" -p "$base_sha" <<EOF
$mutex_message
EOF
  ) || {
    printf 'activation: cannot create the activation mutex commit\n' >&2
    return 2
  }

  if ! git -c core.hooksPath=/dev/null push --quiet --force-with-lease="$MUTEX_REF:" \
    "$ORIGIN_PUSH_URL" "$MUTEX_SHA:$MUTEX_REF"; then
    mutex_observed_lines=$(git ls-remote --heads "$ORIGIN_URL" "$MUTEX_REF" 2>/dev/null) || {
      printf 'activation: mutex push failed and its remote state cannot be inspected\n' >&2
      return 2
    }
    mutex_observed=$(printf '%s\n' "$mutex_observed_lines" | awk 'NF { print $1; exit }')
    if [ "$mutex_observed" = "$MUTEX_SHA" ]; then
      printf 'activation: mutex push response was uncertain, but this updater owns it\n' >&2
    elif [ -n "$mutex_observed" ]; then
      if [ "${AI_TEAM_RECOVER_MUTEX_SHA:-}" = "$mutex_observed" ]; then
        git fetch -q --no-tags origin "$MUTEX_BRANCH" || {
          printf 'activation: cannot fetch the exact mutex selected for recovery\n' >&2
          return 2
        }
        stale_message=$(git show -s --format=%B "$mutex_observed" 2>/dev/null || true)
        stale_managed=$(printf '%s\n' "$stale_message" | awk -F': ' '/^AI-Team-Activation-Mutex: / { value=$2 } END { print value }')
        stale_base=$(printf '%s\n' "$stale_message" | awk -F': ' '/^AI-Team-Activation-Mutex-Base: / { value=$2 } END { print value }')
        stale_owner=$(printf '%s\n' "$stale_message" | awk -F': ' '/^AI-Team-Activation-Mutex-Owner: / { value=$2 } END { print value }')
        stale_nonce=$(printf '%s\n' "$stale_message" | awk -F': ' '/^AI-Team-Activation-Mutex-Nonce: / { value=$2 } END { print value }')
        stale_parent_line=$(git rev-list --parents -n 1 "$mutex_observed" 2>/dev/null || true)
        stale_parent=$(printf '%s\n' "$stale_parent_line" | awk 'NF == 2 { print $2 }')
        stale_tree=$(git rev-parse "$mutex_observed^{tree}" 2>/dev/null || true)
        stale_base_tree=$(git rev-parse "$stale_base^{tree}" 2>/dev/null || true)
        [ "$stale_managed" = "true" ] && [ "$stale_base" = "$stale_parent" ] && \
          [ -n "$stale_tree" ] && [ "$stale_tree" = "$stale_base_tree" ] && \
          printf '%s\n' "$stale_base" | grep -Eq '^[0-9a-fA-F]{40,64}$' && \
          printf '%s\n' "$stale_nonce" | grep -Eq '^[0-9a-fA-F]{32}$' && \
          printf '%s\n' "$stale_owner" | grep -Eq '^(claim:[a-z0-9]+([._-][a-z0-9]+)*:#[0-9]+|package-refresh:[0-9a-fA-F]{40,64})$' || {
          printf 'activation: selected mutex is malformed or not generated state; refusing recovery\n' >&2
          return 2
        }
        if ! git -c core.hooksPath=/dev/null push --quiet --force-with-lease="$MUTEX_REF:$mutex_observed" \
          "$ORIGIN_PUSH_URL" ":$MUTEX_REF"; then
          recovered_lines=$(git ls-remote --heads "$ORIGIN_URL" "$MUTEX_REF" 2>/dev/null) || return 2
          recovered_observed=$(printf '%s\n' "$recovered_lines" | awk 'NF { print $1; exit }')
          [ -z "$recovered_observed" ] || {
            printf 'activation: mutex changed during exact recovery; refusing to delete %s\n' "$recovered_observed" >&2
            return 2
          }
        fi
        printf 'activation: recovered exact stale generated mutex %s after owner verification\n' "$mutex_observed" >&2
        MUTEX_EMPTY_RETRY_COUNT=0
        acquire_activation_mutex
        return $?
      fi
      mutex_wait_limit=${AI_TEAM_MUTEX_WAIT_SECONDS:-30}
      printf '%s\n' "$mutex_wait_limit" | grep -Eq '^[0-9]+$' || {
        printf 'activation: AI_TEAM_MUTEX_WAIT_SECONDS must be a non-negative integer\n' >&2
        return 2
      }
      mutex_waited=0
      while [ "$mutex_waited" -lt "$mutex_wait_limit" ]; do
        sleep 1
        mutex_waited=$((mutex_waited + 1))
        waited_lines=$(git ls-remote --heads "$ORIGIN_URL" "$MUTEX_REF" 2>/dev/null) || return 2
        waited_observed=$(printf '%s\n' "$waited_lines" | awk 'NF { print $1; exit }')
        if [ -z "$waited_observed" ]; then
          printf 'activation: the short mutex cleared after %s second(s); observing the durable lane\n' "$mutex_waited" >&2
          MUTEX_EMPTY_RETRY_COUNT=0
          acquire_activation_mutex
          return $?
        fi
        mutex_observed=$waited_observed
      done
      # A package activation can legitimately hold this short mutex longer
      # than the local wait budget while it repairs historical merged lanes.
      # If its exact generated owner names this same immutable package
      # revision, pause onboarding with a durable, revision-tied result rather
      # than turning scheduler timing into a false activation failure.
      if git fetch -q --no-tags origin "$mutex_observed" 2>/dev/null; then
        waited_message=$(git show -s --format=%B "$mutex_observed" 2>/dev/null || true)
        waited_managed=$(printf '%s\n' "$waited_message" | awk -F': ' '/^AI-Team-Activation-Mutex: / { value=$2 } END { print value }')
        waited_base=$(printf '%s\n' "$waited_message" | awk -F': ' '/^AI-Team-Activation-Mutex-Base: / { value=$2 } END { print value }')
        waited_owner=$(printf '%s\n' "$waited_message" | awk -F': ' '/^AI-Team-Activation-Mutex-Owner: / { value=$2 } END { print value }')
        waited_nonce=$(printf '%s\n' "$waited_message" | awk -F': ' '/^AI-Team-Activation-Mutex-Nonce: / { value=$2 } END { print value }')
        waited_parent_line=$(git rev-list --parents -n 1 "$mutex_observed" 2>/dev/null || true)
        waited_parent=$(printf '%s\n' "$waited_parent_line" | awk 'NF == 2 { print $2 }')
        waited_tree=$(git rev-parse "$mutex_observed^{tree}" 2>/dev/null || true)
        waited_base_tree=$(git rev-parse "$waited_base^{tree}" 2>/dev/null || true)
        if [ "$waited_managed" = "true" ] && [ "$waited_base" = "$waited_parent" ] && \
           [ -n "$waited_tree" ] && [ "$waited_tree" = "$waited_base_tree" ] && \
           printf '%s\n' "$waited_base" | grep -Eq '^[0-9a-fA-F]{40,64}$' && \
           printf '%s\n' "$waited_nonce" | grep -Eq '^[0-9a-fA-F]{32}$' && \
           [ "$waited_owner" = "package-refresh:$UPSTREAM_SHA" ]; then
          stop_for_pr "package revision $UPSTREAM_SHA is in progress under exact activation mutex $mutex_observed"
        fi
      fi
      printf 'activation: another activation or claim owns origin/%s; retry after it finishes\n' "$MUTEX_BRANCH" >&2
      printf 'activation: if no process is running, an owner may verify it and rerun with AI_TEAM_RECOVER_MUTEX_SHA=%s; activation never steals it\n' "$mutex_observed" >&2
      return 1
    else
      if [ "$MUTEX_EMPTY_RETRY_COUNT" -lt 3 ]; then
        MUTEX_EMPTY_RETRY_COUNT=$((MUTEX_EMPTY_RETRY_COUNT + 1))
        printf 'activation: mutex CAS failed but the ref is now empty; retrying with a fresh nonce (%s/3)\n' \
          "$MUTEX_EMPTY_RETRY_COUNT" >&2
        acquire_activation_mutex
        return $?
      fi
      printf 'activation: cannot acquire origin/%s (check push permission or protection)\n' "$MUTEX_BRANCH" >&2
      return 2
    fi
  fi
  MUTEX_EMPTY_RETRY_COUNT=0
  MUTEX_HELD=1
}

release_activation_mutex() {
  [ "$MUTEX_HELD" -eq 1 ] || return 0
  if git -c core.hooksPath=/dev/null push --quiet --force-with-lease="$MUTEX_REF:$MUTEX_SHA" \
    "$ORIGIN_PUSH_URL" ":$MUTEX_REF"; then
    MUTEX_HELD=0
    return 0
  fi
  mutex_observed_lines=$(git ls-remote --heads "$ORIGIN_URL" "$MUTEX_REF" 2>/dev/null) || {
    printf 'activation: cannot release the activation mutex or inspect its remote state\n' >&2
    return 2
  }
  mutex_observed=$(printf '%s\n' "$mutex_observed_lines" | awk 'NF { print $1; exit }')
  if [ -z "$mutex_observed" ]; then
    MUTEX_HELD=0
    return 0
  fi
  if [ "$mutex_observed" != "$MUTEX_SHA" ]; then
    printf 'activation: mutex ownership changed unexpectedly; refusing to delete %s\n' "$mutex_observed" >&2
  else
    printf 'activation: cannot release origin/%s; delete only that exact generated ref after verifying no activation or claim is running\n' "$MUTEX_BRANCH" >&2
  fi
  return 2
}

mark_failed_refresh_claim() {
  [ "$PUBLISHED_CLAIM_ACTIVE" -eq 1 ] && [ -n "$PUBLISHED_CLAIM_SHA" ] || return 0
  failed_observed_lines=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || return 1
  failed_observed=$(printf '%s\n' "$failed_observed_lines" | awk 'NF { print $1; exit }')
  [ "$failed_observed" = "$PUBLISHED_CLAIM_SHA" ] || return 1
  failed_tree=$(git rev-parse "$base_sha^{tree}" 2>/dev/null) || return 1
  failed_message=$(printf 'AI Team package refresh stopped before verification\n\nAI-Team-Activation-Managed: true\nAI-Team-Activation-Base: %s\nAI-Team-Package-Source: %s\nAI-Team-Package-Ref: %s\nAI-Team-Package-Revision: %s\nAI-Team-Activation-Failed: true\n' \
    "$base_sha" "$SOURCE" "$REF" "$UPSTREAM_SHA")
  failed_sha=$(git -c user.name=ai-team-package-bootstrap \
    -c user.email=ai-team-package-bootstrap@users.noreply.github.com \
    commit-tree "$failed_tree" -p "$base_sha" <<EOF
$failed_message
EOF
  ) || return 1
  if git -c core.hooksPath=/dev/null push --quiet --force-with-lease="refs/heads/$UPDATE_BRANCH:$PUBLISHED_CLAIM_SHA" \
    "$ORIGIN_PUSH_URL" "$failed_sha:refs/heads/$UPDATE_BRANCH"; then
    PUBLISHED_CLAIM_ACTIVE=0
    printf 'activation: recorded a recoverable failed refresh marker at %s\n' "$failed_sha" >&2
    return 0
  fi
  failed_after_lines=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || return 1
  failed_after=$(printf '%s\n' "$failed_after_lines" | awk 'NF { print $1; exit }')
  [ "$failed_after" = "$failed_sha" ] || return 1
  PUBLISHED_CLAIM_ACTIVE=0
  return 0
}

activation_cleanup() {
  cleanup_status=$?
  trap - EXIT HUP INT TERM
  if [ "$cleanup_status" -ne 0 ] && ! mark_failed_refresh_claim; then
    printf 'activation: could not convert this invocation\047s claim into a recoverable failure marker; verify no updater is running before exact recovery of %s\n' "$PUBLISHED_CLAIM_SHA" >&2
  fi
  if [ "$WORKTREE_ADDED" -eq 1 ]; then
    git -C "$ROOT" worktree remove --force "$TEMP_WORKTREE" >/dev/null 2>&1 || true
  fi
  if [ -n "$TEMP_SUITE_REPO" ] && [ -n "$TEMP_PARENT" ]; then
    case "$TEMP_SUITE_REPO" in
      "$TEMP_PARENT"/suite-repo) rm -rf -- "$TEMP_SUITE_REPO" ;;
      *) printf 'activation: refusing cleanup of unexpected suite path %s\n' "$TEMP_SUITE_REPO" >&2 ;;
    esac
  fi
  [ -z "$BODY_FILE" ] || [ ! -f "$BODY_FILE" ] || rm -f -- "$BODY_FILE"
  [ -z "$TEMP_PARENT" ] || rmdir "$TEMP_PARENT" >/dev/null 2>&1 || true
  if ! release_activation_mutex; then
    [ "$cleanup_status" -ne 0 ] || cleanup_status=2
  fi
  exit "$cleanup_status"
}

[ -f "$CONFIG" ] || fail "$CONFIG is missing; install the owner-reviewed activation policy first"

SOURCE=
REF=
seen_source=0
seen_ref=0
while IFS= read -r policy_line || [ -n "$policy_line" ]; do
  policy_line=${policy_line%$'\r'}
  case "$policy_line" in
    ''|'#'*) continue ;;
    *=*) ;;
    *) fail "$CONFIG contains a line without key=value" ;;
  esac

  policy_key=${policy_line%%=*}
  policy_value=${policy_line#*=}
  [ -n "$policy_value" ] || fail "$CONFIG has an empty $policy_key value"
  case "$policy_key" in
    source)
      [ "$seen_source" -eq 0 ] || fail "$CONFIG repeats source"
      SOURCE=$policy_value
      seen_source=1
      ;;
    ref)
      [ "$seen_ref" -eq 0 ] || fail "$CONFIG repeats ref"
      REF=$policy_value
      seen_ref=1
      ;;
    *) fail "$CONFIG contains unsupported key $policy_key" ;;
  esac
done < "$CONFIG"

[ "$seen_source" -eq 1 ] || fail "$CONFIG must declare source"
[ "$seen_ref" -eq 1 ] || fail "$CONFIG must declare ref"
git check-ref-format "refs/heads/$REF" >/dev/null 2>&1 || \
  fail "$CONFIG contains an unsafe ref"

ORIGIN_URL=$(git remote get-url origin 2>/dev/null) || fail "origin remote is missing"
ORIGIN_PUSH_URL=$(git remote get-url --push origin 2>/dev/null) || \
  fail "origin has no writable push URL"
repo_info=$(gh repo view "$ORIGIN_URL" --json nameWithOwner,defaultBranchRef \
  --jq '[.nameWithOwner, .defaultBranchRef.name] | @tsv' 2>/dev/null) || \
  fail "cannot resolve origin through gh"
IFS=$'\t' read -r REPO BASE <<EOF
$repo_info
EOF
[ -n "${REPO:-}" ] || fail "gh returned no repository for origin"
[ -n "${BASE:-}" ] || fail "gh returned no default branch for origin"
push_repo=$(gh repo view "$ORIGIN_PUSH_URL" --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null) || \
  fail "cannot resolve origin's push URL through gh"
[ "$push_repo" = "$REPO" ] || \
  fail "origin fetch and push URLs resolve to different repositories"

upstream_lines=$(git ls-remote --exit-code "$SOURCE" "refs/heads/$REF" 2>/dev/null) || \
  fail "cannot resolve approved package ref $REF from $SOURCE"
upstream_count=$(printf '%s\n' "$upstream_lines" | awk 'NF { count++ } END { print count + 0 }')
[ "$upstream_count" -eq 1 ] || fail "approved package ref $REF did not resolve uniquely"
UPSTREAM_SHA=$(printf '%s\n' "$upstream_lines" | awk 'NF { print $1 }')
printf '%s\n' "$UPSTREAM_SHA" | grep -Eq '^[0-9a-fA-F]{40,64}$' || \
  fail "approved package ref $REF returned an invalid revision"

# Fetch the immutable object named above. FETCH_HEAD is not used afterwards;
# concurrent activations may update it without changing the resolved object.
git fetch -q --no-tags "$SOURCE" "$UPSTREAM_SHA" || \
  fail "cannot fetch package revision $UPSTREAM_SHA"
UPSTREAM_TREE=$(git rev-parse "$UPSTREAM_SHA^{tree}" 2>/dev/null) || \
  fail "package revision $UPSTREAM_SHA has no tree"

run_mounted_suite() {
  suite_checkout=$1
  suite_python=
  if command -v python3 >/dev/null 2>&1 && python3 --version >/dev/null 2>&1; then
    suite_python=python3
  elif command -v python >/dev/null 2>&1 && python --version >/dev/null 2>&1; then
    suite_python=python
  else
    printf 'activation: Python is required to verify the mounted mechanism suite\n' >&2
    return 1
  fi
  (cd "$suite_checkout" && \
    "$suite_python" -B -m unittest discover -s "$PREFIX/scripts" -p 'test_*.py')
}

remove_isolated_suite_repo() {
  [ -n "$TEMP_SUITE_REPO" ] && [ -n "$TEMP_PARENT" ] || return 0
  case "$TEMP_SUITE_REPO" in
    "$TEMP_PARENT"/suite-repo) rm -rf -- "$TEMP_SUITE_REPO" || return 1 ;;
    *) return 1 ;;
  esac
  TEMP_SUITE_REPO=
}

# Run package tests in a standalone repository, never a linked worktree. This
# prevents a passing suite from changing the caller/build repository's shared
# hooks, config, refs, index, or object namespace. Return 1 for an ordinary
# failing suite and 2 for setup, mutation, or cleanup violations.
run_isolated_mounted_suite() {
  isolated_revision=$1
  isolated_tree=$2
  isolated_source_checkout=$3
  TEMP_SUITE_REPO="$TEMP_PARENT/suite-repo"
  [ ! -e "$TEMP_SUITE_REPO" ] || return 2
  git init -q "$TEMP_SUITE_REPO" || return 2
  git -C "$TEMP_SUITE_REPO" config user.name ai-team-suite || return 2
  git -C "$TEMP_SUITE_REPO" config user.email ai-team-suite@users.noreply.github.com || return 2
  git -C "$TEMP_SUITE_REPO" -c protocol.file.allow=always \
    fetch -q --no-tags "$isolated_source_checkout" HEAD || return 2
  git -C "$TEMP_SUITE_REPO" -c core.hooksPath=/dev/null checkout -q --detach FETCH_HEAD || return 2
  git -C "$TEMP_SUITE_REPO" remote add origin "$ORIGIN_URL" || return 2
  git -C "$TEMP_SUITE_REPO" remote set-url --push origin "$TEMP_PARENT/non-writable-origin" || return 2

  isolated_before_head=$(git -C "$TEMP_SUITE_REPO" rev-parse HEAD 2>/dev/null || true)
  isolated_before_tree=$(git -C "$TEMP_SUITE_REPO" rev-parse "HEAD^{tree}" 2>/dev/null || true)
  isolated_before_prefix=$(git -C "$TEMP_SUITE_REPO" rev-parse "HEAD:$PREFIX" 2>/dev/null || true)
  isolated_config_before=$(git -C "$TEMP_SUITE_REPO" config --local --list 2>/dev/null | LC_ALL=C sort)
  isolated_refs_before=$(git -C "$TEMP_SUITE_REPO" for-each-ref --format='%(refname)%09%(objectname)' 2>/dev/null | LC_ALL=C sort)
  isolated_hooks_before=$(find "$TEMP_SUITE_REPO/.git/hooks" -type f ! -name '*.sample' -print 2>/dev/null | LC_ALL=C sort)
  [ "$isolated_before_head" = "$isolated_revision" ] && \
    [ "$isolated_before_tree" = "$isolated_tree" ] && \
    [ "$isolated_before_prefix" = "$UPSTREAM_TREE" ] && \
    [ -z "$(git -C "$TEMP_SUITE_REPO" status --porcelain --untracked-files=all)" ] || \
    return 2

  run_mounted_suite "$TEMP_SUITE_REPO"
  isolated_suite_status=$?
  [ "$isolated_suite_status" -eq 0 ] || return 1

  isolated_after_head=$(git -C "$TEMP_SUITE_REPO" rev-parse HEAD 2>/dev/null || true)
  isolated_after_tree=$(git -C "$TEMP_SUITE_REPO" rev-parse "HEAD^{tree}" 2>/dev/null || true)
  isolated_after_prefix=$(git -C "$TEMP_SUITE_REPO" rev-parse "HEAD:$PREFIX" 2>/dev/null || true)
  isolated_config_after=$(git -C "$TEMP_SUITE_REPO" config --local --list 2>/dev/null | LC_ALL=C sort)
  isolated_refs_after=$(git -C "$TEMP_SUITE_REPO" for-each-ref --format='%(refname)%09%(objectname)' 2>/dev/null | LC_ALL=C sort)
  isolated_hooks_after=$(find "$TEMP_SUITE_REPO/.git/hooks" -type f ! -name '*.sample' -print 2>/dev/null | LC_ALL=C sort)
  [ "$isolated_after_head" = "$isolated_revision" ] && \
    [ "$isolated_after_tree" = "$isolated_tree" ] && \
    [ "$isolated_after_prefix" = "$UPSTREAM_TREE" ] && \
    [ "$isolated_config_after" = "$isolated_config_before" ] && \
    [ "$isolated_refs_after" = "$isolated_refs_before" ] && \
    [ "$isolated_hooks_after" = "$isolated_hooks_before" ] && \
    [ -z "$(git -C "$TEMP_SUITE_REPO" status --porcelain --untracked-files=all)" ] || \
    return 2
  remove_isolated_suite_repo || return 2
  return 0
}

refresh_remote_update_sha() {
  remote_update_line=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
    return 1
  REMOTE_UPDATE_SHA=$(printf '%s\n' "$remote_update_line" | awk 'NF { print $1; exit }')
}

scan_active_lanes() {
  # A blocked backlog issue does not mutate a checkout. Active/review issues
  # block refresh, and every open PR (including a blocked lane's PR) blocks it.
  # Only the exact canonical generated refresh lane is exempt.
  scanned_issues=$(gh issue list --repo "$REPO" --state open --limit 1000 \
    --json number,labels \
    --jq '.[] | select(any(.labels[]?; .name == "task:active" or .name == "task:review")) | "#\(.number)"' \
    2>/dev/null) || return 1
  scanned_pr_rows=$(gh pr list --repo "$REPO" --state open --limit 1000 \
    --json number,headRefOid,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,labels,body \
    --jq '.[] | [.number, .headRefOid, .headRefName, .baseRefName, ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' \
    2>/dev/null) || return 1
  scanned_prs=$(printf '%s\n' "$scanned_pr_rows" | awk -F'\t' \
    -v head="$REMOTE_UPDATE_SHA" -v repo="$REPO" -v base="$BASE" -v branch="$UPDATE_BRANCH" \
    -v agent="agent:$BOOTSTRAP_AGENT" \
    'NF && !($2 != "" && $2 == head && $3 == branch && $4 == base && $5 == repo && $6 == "false" && $7 == agent && $8 == "true" && $9 == "true") { print "PR #" $1 }')
  printf '%s\n%s\n' "$scanned_issues" "$scanned_prs" | awk 'NF'
}

# GitHub does not always delete a merged PR's head branch. Remove that branch
# only after its commit metadata, mounted tree, exact merged PR, and terminal
# labels prove it is generated state already present on the default branch.
# The caller must own the activation mutex.
cleanup_proven_merged_refresh() {
  lingering_line=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
    fail "cannot inspect origin/$UPDATE_BRANCH"
  lingering_sha=$(printf '%s\n' "$lingering_line" | awk 'NF { print $1; exit }')
  [ -n "$lingering_sha" ] || return 0

  open_rows=$(gh pr list --repo "$REPO" --state open \
    --head "$UPDATE_BRANCH" --limit 1000 \
    --json number,headRefOid,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository \
    --jq '.[] | [.number, .headRefOid, ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName] | @tsv' \
    2>/dev/null) || \
    fail "cannot inspect whether origin/$UPDATE_BRANCH still has an open PR"
  open_lingering=$(printf '%s\n' "$open_rows" | awk -F'\t' \
    -v head="$lingering_sha" -v repo="$REPO" -v branch="$UPDATE_BRANCH" \
    '$2 == head && $3 == repo && $4 == "false" && $6 == branch { count++ } END { print count + 0 }')
  [ "$open_lingering" = "0" ] || return 0

  git fetch -q --no-tags origin "$UPDATE_BRANCH" || \
    fail "cannot fetch lingering origin/$UPDATE_BRANCH"
  lingering_message=$(git show -s --format=%B "$lingering_sha" 2>/dev/null) || \
    fail "cannot inspect lingering origin/$UPDATE_BRANCH metadata"
  lingering_managed=$(printf '%s\n' "$lingering_message" | awk -F': ' '/^AI-Team-Activation-Managed: / { value=$2 } END { print value }')
  lingering_base=$(printf '%s\n' "$lingering_message" | awk -F': ' '/^AI-Team-Activation-Base: / { value=$2 } END { print value }')
  lingering_source=$(printf '%s\n' "$lingering_message" | awk -F': ' '/^AI-Team-Package-Source: / { value=$2 } END { print value }')
  lingering_ref=$(printf '%s\n' "$lingering_message" | awk -F': ' '/^AI-Team-Package-Ref: / { value=$2 } END { print value }')
  lingering_revision=$(printf '%s\n' "$lingering_message" | awk -F': ' '/^AI-Team-Package-Revision: / { value=$2 } END { print value }')
  lingering_claim=$(printf '%s\n' "$lingering_message" | awk -F': ' '/^AI-Team-Activation-Claim: / { value=$2 } END { print value }')
  lingering_failed=$(printf '%s\n' "$lingering_message" | awk -F': ' '/^AI-Team-Activation-Failed: / { value=$2 } END { print value }')
  # Pending/failed commits are not merge-cleanup candidates. Leave them for
  # the exact recovery state machine below; otherwise an interrupted updater
  # with no PR could never reach its owner-guided recovery path.
  [ -z "$lingering_failed" ] || return 0
  [ -z "$lingering_claim" ] || return 0
  [ "${AI_TEAM_RECOVER_REFRESH_SHA:-}" != "$lingering_sha" ] || return 0
  [ "$lingering_managed" = "true" ] && [ -z "$lingering_claim" ] && \
    [ "$lingering_source" = "$SOURCE" ] && [ "$lingering_ref" = "$REF" ] || \
    fail "closed origin/$UPDATE_BRANCH is not a completed activation-owned branch; refusing to delete it"
  printf '%s\n' "$lingering_base" | grep -Eq '^[0-9a-fA-F]{40,64}$' && \
    git cat-file -e "$lingering_base^{commit}" 2>/dev/null && \
    git merge-base --is-ancestor "$lingering_base" "$lingering_sha" 2>/dev/null || \
    fail "closed origin/$UPDATE_BRANCH has invalid activation ancestry; refusing to delete it"
  git diff --quiet "$lingering_base" "$lingering_sha" -- . ":(exclude)$PREFIX" || \
    fail "closed origin/$UPDATE_BRANCH changes adopter-owned paths outside $PREFIX; refusing to delete or overwrite it"
  printf '%s\n' "$lingering_revision" | grep -Eq '^[0-9a-fA-F]{40,64}$' || \
    fail "closed origin/$UPDATE_BRANCH has an invalid package revision"
  git fetch -q --no-tags "$SOURCE" "$lingering_revision" || \
    fail "cannot fetch the package revision recorded by closed origin/$UPDATE_BRANCH"
  lingering_package_tree=$(git rev-parse "$lingering_revision^{tree}" 2>/dev/null) || \
    fail "the package revision recorded by closed origin/$UPDATE_BRANCH has no tree"
  lingering_tree=$(git rev-parse "$lingering_sha:$PREFIX" 2>/dev/null || true)
  [ -n "$lingering_tree" ] && [ "$lingering_tree" = "$lingering_package_tree" ] || \
    fail "closed origin/$UPDATE_BRANCH does not contain its exact recorded package tree"

  merged_rows=$(gh pr list --repo "$REPO" --state merged \
    --head "$UPDATE_BRANCH" --limit 1000 \
    --json number,headRefOid,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,mergeCommit,labels,body \
    --jq '.[] | [.number, .headRefOid, (.mergeCommit.oid // "-"), ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' \
    2>/dev/null) || fail "cannot inspect the merged PR for origin/$UPDATE_BRANCH"
  merged_same_head=$(printf '%s\n' "$merged_rows" | awk -F'\t' \
    -v head="$lingering_sha" -v repo="$REPO" -v branch="$UPDATE_BRANCH" \
    '$2 == head && $4 == repo && $5 == "false" && $7 == branch')
  merged_same_head_count=$(printf '%s\n' "$merged_same_head" | awk 'NF { count++ } END { print count + 0 }')
  [ "$merged_same_head_count" -le 1 ] || \
    fail "closed origin/$UPDATE_BRANCH is reused by multiple same-repository merged PRs; refusing terminalization or deletion"
  merged_info=$(printf '%s\n' "$merged_same_head" | awk -F'\t' -v base="$BASE" '$6 == base')
  merged_count=$(printf '%s\n' "$merged_info" | awk 'NF { count++ } END { print count + 0 }')
  # A verified branch whose PR was closed/deleted before merge is owner-
  # recoverable, not deletion-safe. Defer it to the exact recovery state
  # machine so the diagnostic names the precise lease SHA.
  [ "$merged_count" -ne 0 ] || return 0
  [ "$merged_count" -eq 1 ] || \
    fail "closed origin/$UPDATE_BRANCH has multiple matching merged PRs; refusing to delete it"
  default_tree=$(git rev-parse "origin/$BASE:$PREFIX" 2>/dev/null || true)
  [ "$lingering_tree" = "$default_tree" ] || \
    fail "closed origin/$UPDATE_BRANCH is not the package tree on origin/$BASE; refusing to delete it"
  IFS=$'\t' read -r merged_number merged_head merged_commit merged_repo merged_cross \
    merged_base merged_branch merged_agents merged_owner merged_issueless <<EOF
$merged_info
EOF
  [ "$merged_head" = "$lingering_sha" ] && \
    [ "$merged_repo" = "$REPO" ] && [ "$merged_cross" = "false" ] && \
    [ "$merged_base" = "$BASE" ] && [ "$merged_branch" = "$UPDATE_BRANCH" ] && \
    [ "$merged_agents" = "agent:$BOOTSTRAP_AGENT" ] && \
    [ "$merged_owner" = "true" ] && [ "$merged_issueless" = "true" ] || \
    fail "the merged PR does not prove sole package-bootstrap ownership of closed origin/$UPDATE_BRANCH"
  printf '%s\n' "$merged_commit" | grep -Eq '^[0-9a-fA-F]{40,64}$' && \
    git cat-file -e "$merged_commit^{commit}" 2>/dev/null && \
    git merge-base --is-ancestor "$merged_commit" "origin/$BASE" 2>/dev/null || \
    fail "the merged PR commit is not on origin/$BASE; refusing to finalize it"
  merged_commit_tree=$(git rev-parse "$merged_commit:$PREFIX" 2>/dev/null || true)
  [ "$merged_commit_tree" = "$lingering_package_tree" ] || \
    fail "the merged PR commit does not contain its exact recorded package tree"
  merged_parent=$(git rev-parse "$merged_commit^1" 2>/dev/null || true)
  [ -n "$merged_parent" ] && \
    git diff --quiet "$merged_parent" "$merged_commit" -- . ":(exclude)$PREFIX" || \
    fail "the merged PR commit changes adopter-owned paths outside $PREFIX"

  gh label create task:done --repo "$REPO" --force --color 0E8A16 \
    --description "Merged and terminal" >/dev/null 2>&1 || \
    fail "cannot create the terminal task label"
  gh pr edit "$merged_number" --repo "$REPO" --add-label task:done >/dev/null 2>&1 || \
    fail "cannot terminalize merged package refresh PR #$merged_number"
  for stale_label in task:ready task:active task:review task:blocked; do
    gh pr edit "$merged_number" --repo "$REPO" --remove-label "$stale_label" >/dev/null 2>&1 || true
  done
  merged_labels=$(gh pr view "$merged_number" --repo "$REPO" --json labels \
    --jq '[.labels[].name | select(startswith("task:"))] | sort | join(",")' 2>/dev/null) || \
    fail "cannot read back merged package refresh PR #$merged_number labels"
  [ "$merged_labels" = "task:done" ] || \
    fail "merged package refresh PR #$merged_number did not reach exact task:done state"

  if ! git -c core.hooksPath=/dev/null push --quiet --force-with-lease="refs/heads/$UPDATE_BRANCH:$lingering_sha" \
    "$ORIGIN_PUSH_URL" ":refs/heads/$UPDATE_BRANCH"; then
    lingering_after=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
      fail "cannot delete or inspect the merged origin/$UPDATE_BRANCH"
    lingering_after_sha=$(printf '%s\n' "$lingering_after" | awk 'NF { print $1; exit }')
    if [ -n "$lingering_after_sha" ]; then
      [ "$lingering_after_sha" = "$lingering_sha" ] && \
        fail "cannot delete merged origin/$UPDATE_BRANCH (check delete permission or protection)"
      fail "origin/$UPDATE_BRANCH changed while its merged state was being finalized; refusing to delete it"
    fi
  fi
  printf 'activation: finalized merged package refresh PR #%s and removed origin/%s\n' \
    "$merged_number" "$UPDATE_BRANCH"
}

find_unterminalized_deleted_refreshes() {
  deleted_rows=$(gh pr list --repo "$REPO" --state merged \
    --head "$UPDATE_BRANCH" --limit 1000 \
    --json number,headRefOid,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,mergeCommit,labels,body \
    --jq '.[] | [.number, .headRefOid, (.mergeCommit.oid // "-"), ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (([.labels[].name | select(startswith("task:"))] | sort | join(",")) | if . == "" then "-" else . end), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null), ([((.body // "") | split("\n")[]) | select(startswith("- Source: `"))][0] // "-"), ([((.body // "") | split("\n")[]) | select(startswith("- Ref: `"))][0] // "-"), ([((.body // "") | split("\n")[]) | select(startswith("- Resolved revision: `"))][0] // "-")] | @tsv' \
    2>/dev/null) || fail "cannot inspect merged package refresh history"
  AUTO_MERGED_ROWS=$(printf '%s\n' "$deleted_rows" | awk -F'\t' \
    -v repo="$REPO" -v base="$BASE" -v branch="$UPDATE_BRANCH" \
    -v source="- Source: \`$SOURCE\`" -v ref="- Ref: \`$REF\`" \
    '$4 == repo && $5 == "false" && $6 == base && $7 == branch && $8 == "agent:package-bootstrap" && $10 == "true" && $11 == "true" && $12 == source && $13 == ref && $9 != "task:done"')
}

terminalize_deleted_refreshes() {
  find_unterminalized_deleted_refreshes
  [ -n "$AUTO_MERGED_ROWS" ] || return 0

  # Read candidates on fd 3 so nested git/gh commands cannot consume the
  # remaining rows from the loop's stdin.
  while IFS=$'\t' read -r deleted_number deleted_head deleted_merge deleted_repo \
    deleted_cross deleted_base_ref deleted_head_ref deleted_agents deleted_tasks \
    deleted_owner deleted_issueless deleted_source deleted_ref deleted_revision_line <&3; do
    [ -n "$deleted_number" ] || continue
    printf '%s\n' "$deleted_number" | grep -Eq '^[0-9]+$' || continue
    printf '%s\n' "$deleted_head" | grep -Eq '^[0-9a-fA-F]{40,64}$' || continue
    printf '%s\n' "$deleted_merge" | grep -Eq '^[0-9a-fA-F]{40,64}$' || continue
    deleted_same_head_count=$(printf '%s\n' "$deleted_rows" | awk -F'\t' \
      -v head="$deleted_head" -v repo="$REPO" -v branch="$UPDATE_BRANCH" \
      '$2 == head && $4 == repo && $5 == "false" && $7 == branch { count++ } END { print count + 0 }')
    [ "$deleted_same_head_count" -eq 1 ] || \
      fail "merged refresh head $deleted_head is reused by multiple same-repository PRs; refusing terminalization"

    # The reusable branch name and mutable body/labels are only a candidate
    # index. Fetch GitHub's immutable PR head and prove the generated commit,
    # package tree, outside-prefix diff, and merge ancestry before editing it.
    pull_head_lines=$(git ls-remote "$ORIGIN_URL" "refs/pull/$deleted_number/head" 2>/dev/null || true)
    pull_head_count=$(printf '%s\n' "$pull_head_lines" | awk 'NF { count++ } END { print count + 0 }')
    pull_head=$(printf '%s\n' "$pull_head_lines" | awk 'NF { print $1; exit }')
    if [ "$pull_head_count" -ne 1 ] || [ "$pull_head" != "$deleted_head" ] || \
       ! git fetch -q --no-tags --no-write-fetch-head origin "$deleted_head" 2>/dev/null || \
       ! git cat-file -e "$deleted_head^{commit}" 2>/dev/null; then
      printf 'activation: ignored merged PR #%s because its immutable head could not be fetched\n' \
        "$deleted_number" >&2
      continue
    fi

    deleted_message=$(git show -s --format=%B "$deleted_head" 2>/dev/null || true)
    deleted_managed=$(printf '%s\n' "$deleted_message" | awk -F': ' '/^AI-Team-Activation-Managed: / { value=$2 } END { print value }')
    deleted_base=$(printf '%s\n' "$deleted_message" | awk -F': ' '/^AI-Team-Activation-Base: / { value=$2 } END { print value }')
    deleted_commit_source=$(printf '%s\n' "$deleted_message" | awk -F': ' '/^AI-Team-Package-Source: / { value=$2 } END { print value }')
    deleted_commit_ref=$(printf '%s\n' "$deleted_message" | awk -F': ' '/^AI-Team-Package-Ref: / { value=$2 } END { print value }')
    deleted_revision=$(printf '%s\n' "$deleted_message" | awk -F': ' '/^AI-Team-Package-Revision: / { value=$2 } END { print value }')
    deleted_claim=$(printf '%s\n' "$deleted_message" | awk -F': ' '/^AI-Team-Activation-Claim: / { value=$2 } END { print value }')
    deleted_failed=$(printf '%s\n' "$deleted_message" | awk -F': ' '/^AI-Team-Activation-Failed: / { value=$2 } END { print value }')
    expected_revision_line=$(printf -- '- Resolved revision: `%s`' "$deleted_revision")
    if [ "$deleted_managed" != "true" ] || [ -n "$deleted_claim" ] || \
       [ -n "$deleted_failed" ] || [ "$deleted_commit_source" != "$SOURCE" ] || \
       [ "$deleted_commit_ref" != "$REF" ] || \
       [ "$deleted_revision_line" != "$expected_revision_line" ]; then
      printf 'activation: ignored merged PR #%s because its head is not exact generated refresh state\n' \
        "$deleted_number" >&2
      continue
    fi
    if ! printf '%s\n' "$deleted_base" | grep -Eq '^[0-9a-fA-F]{40,64}$' || \
       ! printf '%s\n' "$deleted_revision" | grep -Eq '^[0-9a-fA-F]{40,64}$' || \
       ! git cat-file -e "$deleted_base^{commit}" 2>/dev/null || \
       ! git merge-base --is-ancestor "$deleted_base" "$deleted_head" 2>/dev/null || \
       ! git diff --quiet "$deleted_base" "$deleted_head" -- . ":(exclude)$PREFIX"; then
      printf 'activation: ignored merged PR #%s because its generated ancestry or path ownership was not exact\n' \
        "$deleted_number" >&2
      continue
    fi
    if ! git fetch -q --no-tags "$SOURCE" "$deleted_revision" 2>/dev/null; then
      printf 'activation: ignored merged PR #%s because its recorded package revision could not be fetched\n' \
        "$deleted_number" >&2
      continue
    fi
    deleted_package_tree=$(git rev-parse "$deleted_revision^{tree}" 2>/dev/null || true)
    deleted_head_tree=$(git rev-parse "$deleted_head:$PREFIX" 2>/dev/null || true)
    if [ -z "$deleted_package_tree" ] || [ "$deleted_head_tree" != "$deleted_package_tree" ]; then
      printf 'activation: ignored merged PR #%s because its head package tree was not exact\n' \
        "$deleted_number" >&2
      continue
    fi
    if ! git cat-file -e "$deleted_merge^{commit}" 2>/dev/null; then
      git fetch -q --no-tags origin "$deleted_merge" 2>/dev/null || {
        printf 'activation: ignored merged PR #%s because its immutable merge commit could not be fetched\n' \
          "$deleted_number" >&2
        continue
      }
    fi
    deleted_merge_parent=$(git rev-parse "$deleted_merge^1" 2>/dev/null || true)
    deleted_merge_tree=$(git rev-parse "$deleted_merge:$PREFIX" 2>/dev/null || true)
    if [ -z "$deleted_merge_parent" ] || [ "$deleted_merge_tree" != "$deleted_package_tree" ] || \
       ! git merge-base --is-ancestor "$deleted_base" "$deleted_merge" 2>/dev/null || \
       ! git merge-base --is-ancestor "$deleted_merge" "origin/$BASE" 2>/dev/null || \
       ! git diff --quiet "$deleted_merge_parent" "$deleted_merge" -- . ":(exclude)$PREFIX"; then
      printf 'activation: ignored merged PR #%s because its merge is not an exact package-only ancestor of origin/%s\n' \
        "$deleted_number" "$BASE" >&2
      continue
    fi

    gh label create task:done --repo "$REPO" --force --color 0E8A16 \
      --description "Merged and terminal" >/dev/null 2>&1 || \
      fail "cannot create the terminal task label"
    gh pr edit "$deleted_number" --repo "$REPO" --add-label task:done >/dev/null 2>&1 || \
      fail "cannot terminalize auto-deleted package refresh PR #$deleted_number"
    for stale_label in task:ready task:active task:review task:blocked; do
      gh pr edit "$deleted_number" --repo "$REPO" --remove-label "$stale_label" >/dev/null 2>&1 || true
    done
    deleted_labels=$(gh pr view "$deleted_number" --repo "$REPO" --json labels \
      --jq '[.labels[].name | select(startswith("task:"))] | sort | join(",")' 2>/dev/null) || \
      fail "cannot read back auto-deleted package refresh PR #$deleted_number labels"
    [ "$deleted_labels" = "task:done" ] || \
      fail "auto-deleted package refresh PR #$deleted_number did not reach exact task:done state"
    printf 'activation: terminalized auto-deleted merged package refresh PR #%s\n' "$deleted_number"
  done 3<<EOF
$AUTO_MERGED_ROWS
EOF
}

finalize_current_refresh_if_needed() {
  current_lingering=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
    fail "cannot inspect origin/$UPDATE_BRANCH"
  AUTO_MERGED_ROWS=
  [ -n "$current_lingering" ] || find_unterminalized_deleted_refreshes
  [ -n "$current_lingering" ] || [ -n "$AUTO_MERGED_ROWS" ] || return 0
  current_branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
  [ "$current_branch" = "$BASE" ] || \
    fail "the package is current, but $UPDATE_BRANCH remains; finalize it from a clean $BASE checkout"
  [ -z "$(git status --porcelain --untracked-files=all)" ] || \
    fail "the package is current, but $UPDATE_BRANCH remains; finalize it from a clean checkout"
  git fetch -q origin "$BASE" || fail "cannot refresh origin/$BASE while finalizing the refresh lane"
  local_sha=$(git rev-parse HEAD)
  base_sha=$(git rev-parse "origin/$BASE")
  [ "$local_sha" = "$base_sha" ] || \
    fail "the package is current, but $BASE must match origin/$BASE to finalize the refresh lane"
  trap activation_cleanup EXIT
  trap 'exit 130' HUP INT TERM
  acquire_activation_mutex
  mutex_status=$?
  [ "$mutex_status" -eq 0 ] || exit "$mutex_status"
  refresh_remote_update_sha || fail "cannot inspect origin/$UPDATE_BRANCH before finalization"
  active_lanes=$(scan_active_lanes) || fail "cannot inspect active task and PR lanes before finalization"
  [ -z "$active_lanes" ] || \
    fail "package refresh refuses active task lanes: $(printf '%s' "$active_lanes" | tr '\n' ' ')"
  cleanup_proven_merged_refresh
  terminalize_deleted_refreshes
  current_lingering=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
    fail "cannot recheck origin/$UPDATE_BRANCH"
  current_lingering_sha=$(printf '%s\n' "$current_lingering" | awk 'NF { print $1; exit }')
  [ -z "$current_lingering_sha" ] || \
    stop_for_pr "the package tree is current, but exact generated refresh $current_lingering_sha is still open or incomplete; after verifying no updater is running, resume it with AI_TEAM_RECOVER_REFRESH_SHA=$current_lingering_sha"
  release_activation_mutex || fail "cannot release the activation mutex after finalizing the merged refresh"
}

mounted_tree=$(git rev-parse "HEAD:$PREFIX" 2>/dev/null || true)
RECOVER_CURRENT_REFRESH=0
if [ -n "${AI_TEAM_RECOVER_REFRESH_SHA:-}" ]; then
  selected_recovery_lines=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
    fail "cannot inspect the exact refresh selected for recovery"
  selected_recovery_sha=$(printf '%s\n' "$selected_recovery_lines" | awk 'NF { print $1; exit }')
  [ "${AI_TEAM_RECOVER_REFRESH_SHA:-}" != "$selected_recovery_sha" ] || \
    RECOVER_CURRENT_REFRESH=1
fi
if [ "$mounted_tree" = "$UPSTREAM_TREE" ] && [ "$RECOVER_CURRENT_REFRESH" -eq 0 ]; then
  ORIENT="$ROOT/$PREFIX/scripts/orient.sh"
  [ -x "$ORIENT" ] || fail "the current package matches $UPSTREAM_SHA but $ORIENT is not executable"
  git diff --quiet -- "$PREFIX" || \
    fail "the mounted package has unstaged changes; refusing to execute it"
  git diff --cached --quiet -- "$PREFIX" || \
    fail "the mounted package has staged changes; refusing to execute it"
  [ -z "$(git ls-files --others --exclude-standard -- "$PREFIX")" ] || \
    fail "the mounted package has untracked files; refusing to execute it"

  # Tests run in a standalone repository even when the package is current. A
  # linked worktree would share hooks, config, refs, and objects with the caller
  # and therefore would not be an isolation boundary.
  current_suite_head=$(git rev-parse HEAD) || fail "cannot capture the current checkout before verification"
  current_suite_tree=$(git rev-parse "HEAD^{tree}") || fail "cannot capture the current tree before verification"
  TEMP_PARENT=$(mktemp -d "${TMPDIR:-/tmp}/ai-team-current-verify.XXXXXX") || \
    fail "cannot create an isolated current-package verification directory"
  trap activation_cleanup EXIT
  trap 'exit 130' HUP INT TERM
  current_suite_status=0
  run_isolated_mounted_suite "$current_suite_head" "$current_suite_tree" "$ROOT" || current_suite_status=$?
  [ "$current_suite_status" -ne 1 ] || \
    fail "current package matches $UPSTREAM_SHA but its mounted mechanism suite failed"
  [ "$current_suite_status" -eq 0 ] || \
    fail "current mounted mechanism suite modified its standalone repository; refusing onboarding"
  rmdir "$TEMP_PARENT" >/dev/null 2>&1 || \
    fail "cannot remove the current-package verification directory"
  TEMP_PARENT=
  trap - EXIT HUP INT TERM

  finalize_current_refresh_if_needed
  [ "$(git rev-parse HEAD 2>/dev/null || true)" = "$current_suite_head" ] && \
    [ "$(git rev-parse "HEAD:$PREFIX" 2>/dev/null || true)" = "$UPSTREAM_TREE" ] && \
    git diff --quiet -- "$PREFIX" && \
    git diff --cached --quiet -- "$PREFIX" && \
    [ -z "$(git ls-files --others --exclude-standard -- "$PREFIX")" ] && \
    [ -x "$ORIENT" ] || \
    fail "the caller's mounted package changed after verification; refusing to execute it"
  printf 'activation: package is current at %s (%s)\n' "$UPSTREAM_SHA" "$REF"
  exec "$ORIENT"
fi

# The first opt-in/migration can still have a pre-mutex claim.sh mounted. No
# new protocol can atomically exclude a client that does not participate in
# it, so the repository owner must stop claim clients for this one boundary.
# Once this refresh lands, both scripts share the CAS mutex on every run.
mounted_claim_protocol=$(git show "HEAD:$PREFIX/scripts/claim.sh" 2>/dev/null | \
  awk -F= '/^AI_TEAM_ACTIVATION_MUTEX_PROTOCOL=1$/ { print $2; exit }' || true)
if [ "$mounted_claim_protocol" != "1" ]; then
  [ "${AI_TEAM_EXCLUSIVE_FIRST_REFRESH:-}" = "1" ] || \
    fail "first activation/migration is not mutex-compatible yet; stop every claim client, then rerun this owner-controlled bootstrap with AI_TEAM_EXCLUSIVE_FIRST_REFRESH=1"
  printf 'activation: owner acknowledged an exclusive first refresh; legacy claim clients must remain stopped until its PR merges\n'
fi

# From this point onward an update is required. Refuse every caller state in
# which even a temporary refresh lane could surprise ongoing work.
current_branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || \
  fail "package refresh requires the checked-out default branch, not detached HEAD"
[ "$current_branch" = "$BASE" ] || \
  fail "package refresh requires $BASE; current branch is $current_branch"
[ -z "$(git status --porcelain --untracked-files=all)" ] || \
  fail "package refresh refuses a dirty checkout"

for operation_marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  marker_path=$(git rev-parse --git-path "$operation_marker" 2>/dev/null || true)
  [ -z "$marker_path" ] || [ ! -e "$marker_path" ] || \
    fail "package refresh refuses an in-progress Git operation ($operation_marker)"
done

git fetch -q origin "$BASE" || fail "cannot refresh origin/$BASE"
local_sha=$(git rev-parse HEAD)
base_sha=$(git rev-parse "origin/$BASE")
[ "$local_sha" = "$base_sha" ] || \
  fail "$BASE must exactly match origin/$BASE before package refresh"

trap activation_cleanup EXIT
trap 'exit 130' HUP INT TERM
acquire_activation_mutex
mutex_status=$?
[ "$mutex_status" -eq 0 ] || exit "$mutex_status"
refresh_remote_update_sha || fail "cannot inspect origin/$UPDATE_BRANCH"
active_lanes=$(scan_active_lanes) || fail "cannot inspect active task and PR lanes"
[ -z "$active_lanes" ] || \
  fail "package refresh refuses active task lanes: $(printf '%s' "$active_lanes" | tr '\n' ' ')"
cleanup_proven_merged_refresh
terminalize_deleted_refreshes
refresh_remote_update_sha || fail "cannot recheck origin/$UPDATE_BRANCH after merged-lane finalization"
active_lanes=$(scan_active_lanes) || fail "cannot recheck active task and PR lanes"
[ -z "$active_lanes" ] || \
  fail "package refresh refuses active task lanes: $(printf '%s' "$active_lanes" | tr '\n' ' ')"

pr_rows=$(gh pr list --repo "$REPO" --state open --base "$BASE" \
  --head "$UPDATE_BRANCH" --limit 1000 \
  --json number,isDraft,headRefOid,url,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,labels,body \
  --jq '.[] | [.number, .isDraft, .headRefOid, .url, ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (([.labels[].name | select(startswith("task:"))] | sort | join(",")) | if . == "" then "-" else . end), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' 2>/dev/null) || \
  fail "cannot inspect the package refresh PR"
pr_info=$(printf '%s\n' "$pr_rows" | awk -F'\t' \
  -v head="$REMOTE_UPDATE_SHA" -v repo="$REPO" -v base="$BASE" -v branch="$UPDATE_BRANCH" \
  '$3 != "" && $3 == head && $5 == repo && $6 == "false" && $7 == base && $8 == branch')
pr_count=$(printf '%s\n' "$pr_info" | awk 'NF { count++ } END { print count + 0 }')
[ "$pr_count" -le 1 ] || fail "more than one open package refresh PR exists"
PR_NUMBER=
PR_DRAFT=
PR_HEAD=
PR_URL=
PR_REPO=
PR_CROSS=
PR_BASE=
PR_BRANCH=
PR_AGENTS=
PR_TASKS=
PR_OWNER_BODY=
PR_ISSUELESS=
if [ "$pr_count" -eq 1 ]; then
  IFS=$'\t' read -r PR_NUMBER PR_DRAFT PR_HEAD PR_URL PR_REPO PR_CROSS PR_BASE \
    PR_BRANCH PR_AGENTS PR_TASKS PR_OWNER_BODY PR_ISSUELESS <<EOF
$pr_info
EOF
fi
if [ -n "$REMOTE_UPDATE_SHA" ]; then
  git fetch -q --no-tags origin "$UPDATE_BRANCH" || fail "cannot fetch origin/$UPDATE_BRANCH"
  remote_message=$(git show -s --format=%B "$REMOTE_UPDATE_SHA" 2>/dev/null) || \
    fail "cannot inspect origin/$UPDATE_BRANCH metadata"
  remote_managed=$(printf '%s\n' "$remote_message" | awk -F': ' '/^AI-Team-Activation-Managed: / { value=$2 } END { print value }')
  remote_base=$(printf '%s\n' "$remote_message" | awk -F': ' '/^AI-Team-Activation-Base: / { value=$2 } END { print value }')
  remote_source=$(printf '%s\n' "$remote_message" | awk -F': ' '/^AI-Team-Package-Source: / { value=$2 } END { print value }')
  remote_ref=$(printf '%s\n' "$remote_message" | awk -F': ' '/^AI-Team-Package-Ref: / { value=$2 } END { print value }')
  remote_revision=$(printf '%s\n' "$remote_message" | awk -F': ' '/^AI-Team-Package-Revision: / { value=$2 } END { print value }')
  remote_claim=$(printf '%s\n' "$remote_message" | awk -F': ' '/^AI-Team-Activation-Claim: / { value=$2 } END { print value }')
  remote_failed=$(printf '%s\n' "$remote_message" | awk -F': ' '/^AI-Team-Activation-Failed: / { value=$2 } END { print value }')
  [ "$remote_managed" = "true" ] && [ "$remote_source" = "$SOURCE" ] && \
    [ "$remote_ref" = "$REF" ] || \
    fail "origin/$UPDATE_BRANCH is not an activation-owned branch for the approved source/ref; refusing to overwrite it"
  printf '%s\n' "$remote_revision" | grep -Eq '^[0-9a-fA-F]{40,64}$' || \
    fail "origin/$UPDATE_BRANCH has an invalid generated package revision"
  if [ -n "$remote_claim" ]; then
    printf '%s\n' "$remote_claim" | grep -Eq '^[0-9a-fA-F]{32}$' && \
      [ -z "$remote_failed" ] || \
      fail "origin/$UPDATE_BRANCH has malformed or contradictory claim metadata"
  elif [ -n "$remote_failed" ]; then
    [ "$remote_failed" = "true" ] || \
      fail "origin/$UPDATE_BRANCH has malformed failure metadata"
  fi
  printf '%s\n' "$remote_base" | grep -Eq '^[0-9a-fA-F]{40,64}$' && \
    git cat-file -e "$remote_base^{commit}" 2>/dev/null && \
    git merge-base --is-ancestor "$remote_base" "$REMOTE_UPDATE_SHA" 2>/dev/null || \
    fail "origin/$UPDATE_BRANCH does not have valid activation ancestry; refusing to overwrite it"
  git diff --quiet "$remote_base" "$REMOTE_UPDATE_SHA" -- . ":(exclude)$PREFIX" || \
    fail "origin/$UPDATE_BRANCH changes adopter-owned paths outside $PREFIX; refusing to overwrite it"

  update_tree=$(git rev-parse "$REMOTE_UPDATE_SHA:$PREFIX" 2>/dev/null || true)
  remote_commit_tree=$(git rev-parse "$REMOTE_UPDATE_SHA^{tree}" 2>/dev/null || true)
  remote_base_tree=$(git rev-parse "$remote_base^{tree}" 2>/dev/null || true)
  remote_parent_line=$(git rev-list --parents -n 1 "$REMOTE_UPDATE_SHA" 2>/dev/null || true)
  remote_parent=$(printf '%s\n' "$remote_parent_line" | awk 'NF == 2 { print $2 }')
  if [ -n "$remote_claim" ] || [ "$remote_failed" = "true" ]; then
    [ "$remote_parent" = "$remote_base" ] && [ -n "$remote_commit_tree" ] && \
      [ "$remote_commit_tree" = "$remote_base_tree" ] || \
      fail "origin/$UPDATE_BRANCH has a malformed generated claim/failure shape; refusing to overwrite it"
  else
    git fetch -q --no-tags "$SOURCE" "$remote_revision" || \
      fail "cannot fetch the package revision recorded by origin/$UPDATE_BRANCH"
    remote_revision_tree=$(git rev-parse "$remote_revision^{tree}" 2>/dev/null || true)
    [ -n "$remote_revision_tree" ] && [ "$update_tree" = "$remote_revision_tree" ] || \
      fail "origin/$UPDATE_BRANCH does not exactly match its recorded package revision; refusing to overwrite it"
  fi

  if [ -z "$PR_NUMBER" ]; then
    if [ "$remote_failed" = "true" ]; then
      printf 'activation: resuming recoverable failed refresh state at %s\n' "$REMOTE_UPDATE_SHA" >&2
    elif [ -n "$remote_claim" ] && \
         [ "${AI_TEAM_RECOVER_REFRESH_SHA:-}" = "$REMOTE_UPDATE_SHA" ]; then
      printf 'activation: owner selected exact orphan refresh claim %s (recorded revision %s) for recovery to %s\n' \
        "$REMOTE_UPDATE_SHA" "$remote_revision" "$UPSTREAM_SHA" >&2
    elif [ -n "$remote_claim" ]; then
      stop_for_pr "origin/$UPDATE_BRANCH is claimed for recorded package revision $remote_revision but has no PR; after verifying no updater is running, recover it to $UPSTREAM_SHA with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
    elif [ -z "$remote_claim" ] && \
         [ "${AI_TEAM_RECOVER_REFRESH_SHA:-}" = "$REMOTE_UPDATE_SHA" ]; then
      printf 'activation: owner selected exact verified refresh %s without an open PR for recovery\n' \
        "$REMOTE_UPDATE_SHA" >&2
    elif [ -z "$remote_claim" ]; then
      stop_for_pr "origin/$UPDATE_BRANCH is verified generated state but has no open or matching merged PR; after verifying no updater is running, recover it with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
    else
      fail "activation-owned origin/$UPDATE_BRANCH has no open refresh PR; resolve or delete that exact generated branch before retrying"
    fi
  fi
  if [ -n "$PR_NUMBER" ]; then
    [ "$PR_HEAD" = "$REMOTE_UPDATE_SHA" ] && \
      [ "$PR_REPO" = "$REPO" ] && [ "$PR_CROSS" = "false" ] && \
      [ "$PR_BASE" = "$BASE" ] && [ "$PR_BRANCH" = "$UPDATE_BRANCH" ] && \
      [ "$PR_AGENTS" = "agent:$BOOTSTRAP_AGENT" ] && \
      [ "$PR_OWNER_BODY" = "true" ] && [ "$PR_ISSUELESS" = "true" ] || \
      fail "the open refresh PR does not prove sole package-bootstrap ownership of origin/$UPDATE_BRANCH"
  fi

  # A claim is a live updater boundary regardless of the source revision it
  # recorded. Never supersede it merely because the approved source advanced;
  # only an owner-selected exact lease may recover it after proving that the
  # original updater has stopped.
  if [ -n "$remote_claim" ]; then
    if [ "${AI_TEAM_RECOVER_REFRESH_SHA:-}" = "$REMOTE_UPDATE_SHA" ]; then
      printf 'activation: owner selected exact pending refresh claim %s (recorded revision %s) for recovery to %s\n' \
        "$REMOTE_UPDATE_SHA" "$remote_revision" "$UPSTREAM_SHA" >&2
    else
      if [ -n "$PR_NUMBER" ]; then
        stop_for_pr "package refresh PR #$PR_NUMBER owns pending claim $REMOTE_UPDATE_SHA for recorded revision $remote_revision at $PR_URL; after verifying no updater is running, recover it to $UPSTREAM_SHA with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
      fi
      stop_for_pr "origin/$UPDATE_BRANCH owns pending claim $REMOTE_UPDATE_SHA for recorded revision $remote_revision; after verifying no updater is running, recover it to $UPSTREAM_SHA with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
    fi
  fi

  if [ -z "$remote_claim" ] && [ "$remote_failed" != "true" ]; then
    if [ "$update_tree" = "$UPSTREAM_TREE" ]; then
      if [ "${AI_TEAM_RECOVER_REFRESH_SHA:-}" = "$REMOTE_UPDATE_SHA" ]; then
        printf 'activation: owner selected exact verified refresh %s for re-verification/finalization\n' "$REMOTE_UPDATE_SHA" >&2
      elif [ -z "$PR_NUMBER" ]; then
        stop_for_pr "origin/$UPDATE_BRANCH contains the current package but has no open PR; after verifying no updater is running, recover it with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
      elif [ "$PR_DRAFT" = "true" ]; then
        stop_for_pr "verified package refresh PR #$PR_NUMBER is still draft at $PR_URL; after verifying no updater is running, resume exact finalization with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
      elif [ "$PR_TASKS" != "task:review" ]; then
        stop_for_pr "verified package refresh PR #$PR_NUMBER has contradictory task labels ($PR_TASKS); repair exact state with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
      else
        stop_for_pr "package refresh PR #$PR_NUMBER is ready at $PR_URL"
      fi
    elif [ -n "$PR_NUMBER" ] && \
         { [ "$PR_DRAFT" = "true" ] || [ "$PR_TASKS" != "task:review" ]; } && \
         [ "${AI_TEAM_RECOVER_REFRESH_SHA:-}" != "$REMOTE_UPDATE_SHA" ]; then
      stop_for_pr "verified package refresh PR #$PR_NUMBER is not in exact ready/task:review state at older revision $remote_revision; after verifying no updater is still finalizing it, recover exact head $REMOTE_UPDATE_SHA to $UPSTREAM_SHA with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
    elif [ -n "$PR_NUMBER" ] && [ "$PR_DRAFT" != "false" ] && \
         [ "${AI_TEAM_RECOVER_REFRESH_SHA:-}" != "$REMOTE_UPDATE_SHA" ]; then
      stop_for_pr "verified package refresh PR #$PR_NUMBER is not durably ready at $PR_URL; after verifying no updater is running, recover exact head $REMOTE_UPDATE_SHA with AI_TEAM_RECOVER_REFRESH_SHA=$REMOTE_UPDATE_SHA"
    fi
  fi

  # Before replacing any existing branch head, move its exact PR to draft and
  # read it back while the old head is still intact. This prevents exposing a
  # ready PR whose branch contains a new unverified claim.
  if [ -n "$PR_NUMBER" ] && [ "$PR_DRAFT" != "true" ]; then
    gh pr ready "$PR_NUMBER" --repo "$REPO" --undo >/dev/null 2>&1 || \
      fail "could not return exact refresh PR #$PR_NUMBER to draft; origin/$UPDATE_BRANCH was not changed"
    drafted_state=$(gh pr view "$PR_NUMBER" --repo "$REPO" \
      --json headRefOid,isDraft,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,labels,body \
      --jq '[.headRefOid, .isDraft, ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' \
      2>/dev/null) || fail "cannot verify draft transition for exact refresh PR #$PR_NUMBER"
    IFS=$'\t' read -r drafted_head drafted_flag drafted_repo drafted_cross drafted_base \
      drafted_branch drafted_agents drafted_owner drafted_issueless <<EOF
$drafted_state
EOF
    [ "$drafted_head" = "$REMOTE_UPDATE_SHA" ] && [ "$drafted_flag" = "true" ] && \
      [ "$drafted_repo" = "$REPO" ] && [ "$drafted_cross" = "false" ] && \
      [ "$drafted_base" = "$BASE" ] && [ "$drafted_branch" = "$UPDATE_BRANCH" ] && \
      [ "$drafted_agents" = "agent:$BOOTSTRAP_AGENT" ] && \
      [ "$drafted_owner" = "true" ] && [ "$drafted_issueless" = "true" ] || \
      fail "refresh PR #$PR_NUMBER changed while returning it to draft; origin/$UPDATE_BRANCH was not changed"
    PR_DRAFT=true
  fi

  if [ "$remote_failed" = "true" ]; then
    if [ -n "$PR_NUMBER" ] && [ "$PR_DRAFT" != "true" ]; then
      fail "recoverable failed refresh PR #$PR_NUMBER could not be proven draft"
    fi
    if [ "$remote_revision" != "$UPSTREAM_SHA" ]; then
      printf 'activation: superseding exact generated failure marker %s from revision %s with %s\n' \
        "$REMOTE_UPDATE_SHA" "$remote_revision" "$UPSTREAM_SHA" >&2
    else
      printf 'activation: resuming exact generated failure marker %s\n' "$REMOTE_UPDATE_SHA" >&2
    fi
  fi
fi

gh label create "agent:$BOOTSTRAP_AGENT" --repo "$REPO" --force \
  --color 5319e7 --description "AI Team activation package updater" >/dev/null 2>&1 || \
  fail "cannot create the package bootstrap identity label"
gh label create task:active --repo "$REPO" --force \
  --color BFD4F2 --description "Claimed and in progress" >/dev/null 2>&1 || \
  fail "cannot create the active task label"
gh label create task:review --repo "$REPO" --force \
  --color FBCA04 --description "Ready for independent review" >/dev/null 2>&1 || \
  fail "cannot create the review task label"
gh label create task:done --repo "$REPO" --force \
  --color 0E8A16 --description "Merged and terminal" >/dev/null 2>&1 || \
  fail "cannot create the terminal task label"

TEMP_PARENT=$(mktemp -d "${TMPDIR:-/tmp}/ai-team-package-refresh.XXXXXX") || \
  fail "cannot allocate a temporary refresh directory"
TEMP_WORKTREE="$TEMP_PARENT/worktree"
BODY_FILE="$TEMP_PARENT/pr-body.md"

git -c core.hooksPath=/dev/null worktree add --quiet --detach "$TEMP_WORKTREE" "origin/$BASE" || \
  fail "cannot create the isolated package refresh worktree"
WORKTREE_ADDED=1

export GIT_AUTHOR_NAME=ai-team-package-bootstrap
export GIT_AUTHOR_EMAIL=ai-team-package-bootstrap@users.noreply.github.com
export GIT_COMMITTER_NAME=$GIT_AUTHOR_NAME
export GIT_COMMITTER_EMAIL=$GIT_AUTHOR_EMAIL

write_pending_body() {
  cat > "$BODY_FILE" <<EOF
**From:** $BOOTSTRAP_AGENT

AI-Team-Lane-Issue: none

Activation detected a newer approved AI Team package and prepared this dedicated refresh lane.

- Source: \`$SOURCE\`
- Ref: \`$REF\`
- Resolved revision: \`$UPSTREAM_SHA\`
- Mounted prefix: \`$PREFIX\`
- Verification: pending in the isolated refresh worktree. This draft is not ready to merge.

Normal team onboarding and claims remain paused until this PR merges.

**Adopter impact:** the mounted AI Team package advances to the exact revision above; no adopter-owned task files are changed.
EOF
}

write_verified_body() {
  cat > "$BODY_FILE" <<EOF
**From:** $BOOTSTRAP_AGENT

AI-Team-Lane-Issue: none

Activation detected a newer approved AI Team package and prepared this dedicated refresh lane.

- Source: \`$SOURCE\`
- Ref: \`$REF\`
- Resolved revision: \`$UPSTREAM_SHA\`
- Mounted prefix: \`$PREFIX\`
- Verification: the mounted tree exactly matches the resolved revision and its full mechanism suite passed in the isolated refresh worktree.

Normal team onboarding and claims remain paused until this PR merges.

**Adopter impact:** the mounted AI Team package advances to the exact revision above; no adopter-owned task files are changed.
EOF
}

metadata_message() {
  printf '%s\n\n' "chore(ai-team): refresh package to ${UPSTREAM_SHA:0:12}"
  printf 'AI-Team-Activation-Managed: true\n'
  printf 'AI-Team-Activation-Base: %s\n' "$base_sha"
  printf 'AI-Team-Package-Source: %s\n' "$SOURCE"
  printf 'AI-Team-Package-Ref: %s\n' "$REF"
  printf 'AI-Team-Package-Revision: %s\n' "$UPSTREAM_SHA"
}

claim_nonce=$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n') || \
  fail "cannot generate a unique package refresh claim"
printf '%s\n' "$claim_nonce" | grep -Eq '^[0-9a-fA-F]{32}$' || \
  fail "cannot generate a valid package refresh claim"
claim_message() {
  metadata_message
  printf 'AI-Team-Activation-Claim: %s\n' "$claim_nonce"
}

[ -z "$PR_NUMBER" ] || [ "$PR_DRAFT" = "true" ] || \
  fail "existing refresh PR was not proven draft before publishing an unverified claim"

resolve_pr_by_head() {
  expected_head=$1
  resolve_attempt=0
  while [ "$resolve_attempt" -lt 5 ]; do
    resolve_rows=$(gh pr list --repo "$REPO" --state open --base "$BASE" \
      --head "$UPDATE_BRANCH" --limit 1000 \
      --json number,url,headRefOid,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,labels,body \
      --jq '.[] | [.number, .url, .headRefOid, ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' \
      2>/dev/null) || resolve_rows=
    resolved_pr=$(printf '%s\n' "$resolve_rows" | awk -F'\t' \
      -v head="$expected_head" -v repo="$REPO" -v base="$BASE" -v branch="$UPDATE_BRANCH" \
      -v agent="agent:$BOOTSTRAP_AGENT" \
      '$3 == head && $4 == repo && $5 == "false" && $6 == base && $7 == branch && $8 == agent && $9 == "true" && $10 == "true" { print $1 "\t" $2 }')
    resolved_count=$(printf '%s\n' "$resolved_pr" | awk 'NF { count++ } END { print count + 0 }')
    if [ "$resolved_count" -eq 1 ]; then
      printf '%s\n' "$resolved_pr"
      return 0
    fi
    [ "$resolved_count" -eq 0 ] || return 1
    resolve_attempt=$((resolve_attempt + 1))
    sleep 1
  done
  return 1
}

claim_message | git -C "$TEMP_WORKTREE" \
  -c core.hooksPath=/dev/null -c commit.gpgSign=false \
  commit --allow-empty --no-verify --quiet --file=- || \
  fail "cannot create the package refresh claim commit"
claim_sha=$(git -C "$TEMP_WORKTREE" rev-parse HEAD)
if ! git -C "$TEMP_WORKTREE" -c core.hooksPath=/dev/null push --quiet \
  --force-with-lease="refs/heads/$UPDATE_BRANCH:$REMOTE_UPDATE_SHA" \
  "$ORIGIN_PUSH_URL" "$claim_sha:refs/heads/$UPDATE_BRANCH"; then
  observed_lines=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
    fail "claim push failed and origin/$UPDATE_BRANCH cannot be inspected"
  observed=$(printf '%s\n' "$observed_lines" | awk 'NF { print $1; exit }')
  if [ "$observed" = "$claim_sha" ]; then
    printf 'activation: claim push response was uncertain, but origin/%s contains this activation claim\n' "$UPDATE_BRANCH" >&2
  elif [ "$observed" != "$REMOTE_UPDATE_SHA" ]; then
    stop_for_pr "another updater changed origin/$UPDATE_BRANCH to ${observed:-a deleted ref}"
  else
    fail "cannot publish the package refresh claim; origin/$UPDATE_BRANCH remains unchanged (check push permission or protection)"
  fi
fi
lease_sha=$claim_sha
REMOTE_UPDATE_SHA=$claim_sha
PUBLISHED_CLAIM_SHA=$claim_sha
PUBLISHED_CLAIM_ACTIVE=1
write_pending_body
if [ -z "$PR_NUMBER" ]; then
  created_pr=$(gh pr create --repo "$REPO" --base "$BASE" --head "$UPDATE_BRANCH" \
    --draft --title "Refresh mounted AI Team package to ${UPSTREAM_SHA:0:12}" \
    --body-file "$BODY_FILE" --label "agent:$BOOTSTRAP_AGENT" --label task:active \
    2>/dev/null) || true
  resolved_pr=$(resolve_pr_by_head "$claim_sha") || \
    fail "claimed origin/$UPDATE_BRANCH but could not open or resolve its draft PR"
  IFS=$'\t' read -r PR_NUMBER PR_URL <<EOF
$resolved_pr
EOF
  [ -n "$PR_URL" ] || PR_URL=$created_pr
  PR_DRAFT=true
else
  gh pr edit "$PR_NUMBER" --repo "$REPO" \
    --title "Refresh mounted AI Team package to ${UPSTREAM_SHA:0:12}" \
    --body-file "$BODY_FILE" --add-label "agent:$BOOTSTRAP_AGENT" \
    --add-label task:active >/dev/null 2>&1 || \
    fail "claimed origin/$UPDATE_BRANCH but could not mark PR #$PR_NUMBER pending"
  gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label task:review >/dev/null 2>&1 || true
fi
for stale_label in task:ready task:review task:blocked task:done; do
  gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label "$stale_label" >/dev/null 2>&1 || true
done
pending_pr_state=$(gh pr view "$PR_NUMBER" --repo "$REPO" \
  --json headRefOid,isDraft,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,labels,body \
  --jq '[.headRefOid, .isDraft, ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (([.labels[].name | select(startswith("task:"))] | sort | join(",")) | if . == "" then "-" else . end), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' \
  2>/dev/null) || fail "cannot verify pending state of exact refresh PR #$PR_NUMBER"
IFS=$'\t' read -r pending_head pending_draft pending_repo pending_cross pending_base \
  pending_branch pending_agents pending_tasks pending_owner pending_issueless <<EOF
$pending_pr_state
EOF
[ "$pending_head" = "$claim_sha" ] && [ "$pending_draft" = "true" ] && \
  [ "$pending_repo" = "$REPO" ] && [ "$pending_cross" = "false" ] && \
  [ "$pending_base" = "$BASE" ] && [ "$pending_branch" = "$UPDATE_BRANCH" ] && \
  [ "$pending_agents" = "agent:$BOOTSTRAP_AGENT" ] && \
  [ "$pending_tasks" = "task:active" ] && [ "$pending_owner" = "true" ] && \
  [ "$pending_issueless" = "true" ] || \
  fail "refresh PR #$PR_NUMBER did not reach exact canonical draft/task:active state"

# The fixed remote branch is now the activation lock. Scan again so a claim
# that became visible between the first scan and this atomic lock cannot be
# updated underneath.
active_lanes=$(scan_active_lanes) || \
  fail "refresh lock is established, but active lanes could not be rechecked"
[ -z "$active_lanes" ] || \
  fail "refresh lock is established, but a task lane appeared concurrently: $(printf '%s' "$active_lanes" | tr '\n' ' ')"

# The persistent refresh branch and its draft PR now make the update visible
# to old and new claim clients. Release the short common mutex before the
# isolated subtree build and test run.
release_activation_mutex || \
  fail "the refresh lane is visible, but its activation mutex could not be released"

# Always reconstruct from the exact current default branch. An existing refresh
# PR is generated state, so replacing its obsolete tree under a lease cannot
# discard task-owned work.
if git -C "$TEMP_WORKTREE" rev-parse "HEAD:$PREFIX" >/dev/null 2>&1; then
  if ! git -C "$TEMP_WORKTREE" -c core.hooksPath=/dev/null -c commit.gpgSign=false \
    subtree pull --prefix="$PREFIX" "$SOURCE" "$UPSTREAM_SHA" --squash; then
    fail "git subtree pull conflicted; origin/$UPDATE_BRANCH remains draft and the caller checkout is unchanged"
  fi
else
  if ! git -C "$TEMP_WORKTREE" -c core.hooksPath=/dev/null -c commit.gpgSign=false \
    subtree add --prefix="$PREFIX" "$SOURCE" "$UPSTREAM_SHA" --squash; then
    fail "git subtree add failed; origin/$UPDATE_BRANCH remains draft and the caller checkout is unchanged"
  fi
fi

result_tree=$(git -C "$TEMP_WORKTREE" rev-parse "HEAD:$PREFIX" 2>/dev/null || true)
[ "$result_tree" = "$UPSTREAM_TREE" ] || \
  fail "subtree result does not exactly match package revision $UPSTREAM_SHA"
BUILD_SHA=$(git -C "$TEMP_WORKTREE" rev-parse HEAD 2>/dev/null || true)
BUILD_TREE=$(git -C "$TEMP_WORKTREE" rev-parse "HEAD^{tree}" 2>/dev/null || true)
[ -n "$BUILD_SHA" ] && [ -n "$BUILD_TREE" ] && \
  [ -z "$(git -C "$TEMP_WORKTREE" status --porcelain --untracked-files=all)" ] && \
  git -C "$TEMP_WORKTREE" diff --quiet "$base_sha" "$BUILD_SHA" -- . ":(exclude)$PREFIX" || \
  fail "isolated subtree build did not produce exact clean package-only generated state"

isolated_suite_status=0
run_isolated_mounted_suite "$BUILD_SHA" "$BUILD_TREE" "$TEMP_WORKTREE" || isolated_suite_status=$?
[ "$isolated_suite_status" -ne 1 ] || \
  fail "mounted mechanism tests failed; origin/$UPDATE_BRANCH remains draft and the caller checkout is unchanged"
[ "$isolated_suite_status" -eq 0 ] || \
  fail "mounted mechanism suite modified its standalone repository; refusing publication"
post_suite_head=$(git -C "$TEMP_WORKTREE" rev-parse HEAD 2>/dev/null || true)
post_suite_tree=$(git -C "$TEMP_WORKTREE" rev-parse "HEAD^{tree}" 2>/dev/null || true)
post_suite_prefix=$(git -C "$TEMP_WORKTREE" rev-parse "HEAD:$PREFIX" 2>/dev/null || true)
  [ "$post_suite_head" = "$BUILD_SHA" ] && [ "$post_suite_tree" = "$BUILD_TREE" ] && \
  [ "$post_suite_prefix" = "$UPSTREAM_TREE" ] && \
  [ -z "$(git -C "$TEMP_WORKTREE" status --porcelain --untracked-files=all)" ] && \
  git -C "$TEMP_WORKTREE" diff --quiet "$base_sha" "$post_suite_head" -- . ":(exclude)$PREFIX" || \
  fail "mounted mechanism suite modified the isolated refresh result; refusing publication"

expected_metadata=$(metadata_message)
printf '%s\n' "$expected_metadata" | git -C "$TEMP_WORKTREE" \
  -c core.hooksPath=/dev/null -c commit.gpgSign=false \
  commit --allow-empty --no-verify --quiet --file=- || \
  fail "cannot record the verified package revision"
FINAL_SHA=$(git -C "$TEMP_WORKTREE" rev-parse HEAD)
final_parent_line=$(git -C "$TEMP_WORKTREE" rev-list --parents -n 1 "$FINAL_SHA" 2>/dev/null || true)
final_parent=$(printf '%s\n' "$final_parent_line" | awk 'NF == 2 { print $2 }')
final_tree=$(git -C "$TEMP_WORKTREE" rev-parse "$FINAL_SHA^{tree}" 2>/dev/null || true)
final_prefix=$(git -C "$TEMP_WORKTREE" rev-parse "$FINAL_SHA:$PREFIX" 2>/dev/null || true)
final_message=$(git -C "$TEMP_WORKTREE" show -s --format=%B "$FINAL_SHA" 2>/dev/null || true)
[ "$final_parent" = "$BUILD_SHA" ] && [ "$final_tree" = "$BUILD_TREE" ] && \
  [ "$final_prefix" = "$UPSTREAM_TREE" ] && [ "$final_message" = "$expected_metadata" ] && \
  [ -z "$(git -C "$TEMP_WORKTREE" status --porcelain --untracked-files=all)" ] && \
  git -C "$TEMP_WORKTREE" diff --quiet "$base_sha" "$FINAL_SHA" -- . ":(exclude)$PREFIX" || \
  fail "verified refresh metadata commit was not exact generated package-only state"
if ! git -C "$TEMP_WORKTREE" -c core.hooksPath=/dev/null push --quiet \
  --force-with-lease="refs/heads/$UPDATE_BRANCH:$lease_sha" \
  "$ORIGIN_PUSH_URL" "$FINAL_SHA:refs/heads/$UPDATE_BRANCH"; then
  observed_lines=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
    fail "verified-result push failed and origin/$UPDATE_BRANCH cannot be inspected"
  observed=$(printf '%s\n' "$observed_lines" | awk 'NF { print $1; exit }')
  if [ "$observed" = "$FINAL_SHA" ]; then
    printf 'activation: result push response was uncertain, but origin/%s contains this verified result\n' "$UPDATE_BRANCH" >&2
  elif [ "$observed" != "$lease_sha" ]; then
    stop_for_pr "another updater advanced origin/$UPDATE_BRANCH; this isolated result was not published"
  else
    fail "cannot publish the verified package refresh; origin/$UPDATE_BRANCH remains on the claim (check push permission or protection)"
  fi
fi
published_lines=$(git ls-remote --heads "$ORIGIN_URL" "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
  fail "cannot read back the verified origin/$UPDATE_BRANCH publication"
published_count=$(printf '%s\n' "$published_lines" | awk 'NF { count++ } END { print count + 0 }')
published_sha=$(printf '%s\n' "$published_lines" | awk 'NF { print $1; exit }')
[ "$published_count" -eq 1 ] && [ "$published_sha" = "$FINAL_SHA" ] || \
  stop_for_pr "origin/$UPDATE_BRANCH did not retain this exact verified result after publication"
PUBLISHED_CLAIM_ACTIVE=0
REMOTE_UPDATE_SHA=$FINAL_SHA

write_verified_body
if [ -z "$PR_NUMBER" ]; then
  created_pr=$(gh pr create --repo "$REPO" --base "$BASE" --head "$UPDATE_BRANCH" \
    --draft --title "Refresh mounted AI Team package to ${UPSTREAM_SHA:0:12}" \
    --body-file "$BODY_FILE" --label "agent:$BOOTSTRAP_AGENT" --label task:active \
    2>/dev/null) || true
  resolved_pr=$(resolve_pr_by_head "$FINAL_SHA") || \
    fail "published origin/$UPDATE_BRANCH but could not open or resolve its draft PR"
  IFS=$'\t' read -r PR_NUMBER PR_URL <<EOF
$resolved_pr
EOF
  [ -n "$PR_URL" ] || PR_URL=$created_pr
fi

# ready.sh intentionally rejects AI-Team-Lane-Issue: none. Apply its issue-less
# PR transition here: exact identity/body first, then review state, then ready.
gh pr edit "$PR_NUMBER" --repo "$REPO" \
  --title "Refresh mounted AI Team package to ${UPSTREAM_SHA:0:12}" \
  --body-file "$BODY_FILE" --add-label "agent:$BOOTSTRAP_AGENT" \
  --add-label task:review >/dev/null 2>&1 || \
  fail "published the verified refresh but could not update PR #$PR_NUMBER"
for stale_label in task:ready task:active task:blocked task:done; do
  gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label "$stale_label" >/dev/null 2>&1 || true
done
verified_draft_state=$(gh pr view "$PR_NUMBER" --repo "$REPO" \
  --json headRefOid,isDraft,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,labels,body \
  --jq '[.headRefOid, .isDraft, ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (([.labels[].name | select(startswith("task:"))] | sort | join(",")) | if . == "" then "-" else . end), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' \
  2>/dev/null) || fail "cannot verify the exact reviewed draft state of PR #$PR_NUMBER"
IFS=$'\t' read -r verified_head verified_draft verified_repo verified_cross verified_base \
  verified_branch verified_agents verified_tasks verified_owner verified_issueless <<EOF
$verified_draft_state
EOF
[ "$verified_head" = "$FINAL_SHA" ] && [ "$verified_draft" = "true" ] && \
  [ "$verified_repo" = "$REPO" ] && [ "$verified_cross" = "false" ] && \
  [ "$verified_base" = "$BASE" ] && [ "$verified_branch" = "$UPDATE_BRANCH" ] && \
  [ "$verified_agents" = "agent:$BOOTSTRAP_AGENT" ] && \
  [ "$verified_tasks" = "task:review" ] && [ "$verified_owner" = "true" ] && \
  [ "$verified_issueless" = "true" ] || \
  fail "verified refresh PR #$PR_NUMBER did not reach exact canonical draft/task:review state"
gh pr ready "$PR_NUMBER" --repo "$REPO" >/dev/null 2>&1 || \
  fail "published the verified refresh but could not mark PR #$PR_NUMBER ready"

final_pr_state=$(gh pr view "$PR_NUMBER" --repo "$REPO" \
  --json headRefOid,isDraft,headRefName,baseRefName,headRepository,headRepositoryOwner,isCrossRepository,labels,body \
  --jq '[.headRefOid, .isDraft, ((.headRepositoryOwner.login // "") + "/" + (.headRepository.name // "")), .isCrossRepository, .baseRefName, .headRefName, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (([.labels[].name | select(startswith("task:"))] | sort | join(",")) | if . == "" then "-" else . end), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' \
  2>/dev/null) || fail "cannot verify the final state of PR #$PR_NUMBER"
IFS=$'\t' read -r final_pr_head final_pr_draft final_pr_repo final_pr_cross final_pr_base \
  final_pr_branch final_pr_agents final_pr_tasks final_pr_owner final_pr_issueless <<EOF
$final_pr_state
EOF
[ "$final_pr_head" = "$FINAL_SHA" ] && [ "$final_pr_draft" = "false" ] && \
  [ "$final_pr_repo" = "$REPO" ] && [ "$final_pr_cross" = "false" ] && \
  [ "$final_pr_base" = "$BASE" ] && [ "$final_pr_branch" = "$UPDATE_BRANCH" ] && \
  [ "$final_pr_agents" = "agent:$BOOTSTRAP_AGENT" ] && \
  [ "$final_pr_tasks" = "task:review" ] && [ "$final_pr_owner" = "true" ] && \
  [ "$final_pr_issueless" = "true" ] || \
  fail "PR #$PR_NUMBER did not reach the exact package-bootstrap/task:review state; after correcting GitHub access, recover exact refresh $FINAL_SHA with AI_TEAM_RECOVER_REFRESH_SHA=$FINAL_SHA"

stop_for_pr "package revision $UPSTREAM_SHA passed the mounted suite in PR #$PR_NUMBER at $PR_URL"
