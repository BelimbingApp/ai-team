#!/usr/bin/env bash
# Activation boundary for an adopting repository. Copy this file and
# package-refresh.conf to .ai-team/, then run .ai-team/activate.sh instead of
# orient.sh when a team session starts. An update is built and tested away from
# the caller's checkout; onboarding resumes only after its PR has merged.
set -u

PREFIX=docs/ai-team
UPDATE_BRANCH=ai-team/package-refresh
BOOTSTRAP_AGENT=package-bootstrap
CONFIG=.ai-team/package-refresh.conf

fail() {
  printf 'activation: %s\n' "$*" >&2
  exit 1
}

stop_for_pr() {
  printf 'activation: %s\n' "$*" >&2
  printf 'activation: onboarding is paused until the package refresh PR is merged and the default branch is updated locally\n' >&2
  exit 3
}

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || fail "not a git checkout"
cd "$ROOT" || fail "cannot enter repository root"

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
repo_info=$(gh repo view "$ORIGIN_URL" --json nameWithOwner,defaultBranchRef \
  --jq '[.nameWithOwner, .defaultBranchRef.name] | @tsv' 2>/dev/null) || \
  fail "cannot resolve origin through gh"
IFS=$'\t' read -r REPO BASE <<EOF
$repo_info
EOF
[ -n "${REPO:-}" ] || fail "gh returned no repository for origin"
[ -n "${BASE:-}" ] || fail "gh returned no default branch for origin"

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
    "$suite_python" -m unittest discover -s "$PREFIX/scripts" -p 'test_*.py')
}

mounted_tree=$(git rev-parse "HEAD:$PREFIX" 2>/dev/null || true)
if [ "$mounted_tree" = "$UPSTREAM_TREE" ]; then
  ORIENT="$ROOT/$PREFIX/scripts/orient.sh"
  [ -x "$ORIENT" ] || fail "the current package matches $UPSTREAM_SHA but $ORIENT is not executable"
  git diff --quiet -- "$PREFIX" || \
    fail "the mounted package has unstaged changes; refusing to execute it"
  git diff --cached --quiet -- "$PREFIX" || \
    fail "the mounted package has staged changes; refusing to execute it"
  [ -z "$(git ls-files --others --exclude-standard -- "$PREFIX")" ] || \
    fail "the mounted package has untracked files; refusing to execute it"
  run_mounted_suite "$ROOT" || \
    fail "current package matches $UPSTREAM_SHA but its mounted mechanism suite failed"
  printf 'activation: package is current at %s (%s)\n' "$UPSTREAM_SHA" "$REF"
  exec "$ORIENT"
fi

# From this point onward an update is required. Refuse every caller state in
# which even a temporary refresh lane could surprise ongoing work.
current_branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || \
  fail "package refresh requires the checked-out default branch, not detached HEAD"
[ "$current_branch" = "$BASE" ] || \
  fail "package refresh requires $BASE; current branch is $current_branch"
[ -z "$(git status --porcelain --untracked-files=normal)" ] || \
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

scan_active_lanes() {
  scanned_issues=$(gh issue list --repo "$REPO" --state open --limit 1000 \
    --json number,labels \
    --jq '.[] | select(any(.labels[]?; .name == "task:active" or .name == "task:review" or .name == "task:blocked")) | "#\(.number)"' \
    2>/dev/null) || return 1
  scanned_prs=$(gh pr list --repo "$REPO" --state open --limit 1000 \
    --json number,headRefName \
    --jq '.[] | select(.headRefName != "ai-team/package-refresh") | "PR #\(.number)"' \
    2>/dev/null) || return 1
  printf '%s\n%s\n' "$scanned_issues" "$scanned_prs" | awk 'NF'
}

active_lanes=$(scan_active_lanes) || fail "cannot inspect active task and PR lanes"
[ -z "$active_lanes" ] || \
  fail "package refresh refuses active task lanes: $(printf '%s' "$active_lanes" | tr '\n' ' ')"

pr_info=$(gh pr list --repo "$REPO" --state open --base "$BASE" \
  --head "$UPDATE_BRANCH" --limit 2 --json number,isDraft,headRefOid,url,labels,body \
  --jq '.[] | [.number, .isDraft, .headRefOid, .url, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), (((.body // "") | split("\n") | index("**From:** package-bootstrap")) != null), (((.body // "") | split("\n") | index("AI-Team-Lane-Issue: none")) != null)] | @tsv' 2>/dev/null) || \
  fail "cannot inspect the package refresh PR"
pr_count=$(printf '%s\n' "$pr_info" | awk 'NF { count++ } END { print count + 0 }')
[ "$pr_count" -le 1 ] || fail "more than one open package refresh PR exists"
PR_NUMBER=
PR_DRAFT=
PR_HEAD=
PR_URL=
PR_AGENTS=
PR_OWNER_BODY=
PR_ISSUELESS=
if [ "$pr_count" -eq 1 ]; then
  IFS=$'\t' read -r PR_NUMBER PR_DRAFT PR_HEAD PR_URL PR_AGENTS PR_OWNER_BODY PR_ISSUELESS <<EOF
$pr_info
EOF
fi

remote_update_line=$(git ls-remote --heads origin "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
  fail "cannot inspect origin/$UPDATE_BRANCH"
REMOTE_UPDATE_SHA=$(printf '%s\n' "$remote_update_line" | awk 'NF { print $1; exit }')
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
  [ "$remote_managed" = "true" ] && [ "$remote_source" = "$SOURCE" ] && \
    [ "$remote_ref" = "$REF" ] || \
    fail "origin/$UPDATE_BRANCH is not an activation-owned branch for the approved source/ref; refusing to overwrite it"
  printf '%s\n' "$remote_base" | grep -Eq '^[0-9a-fA-F]{40,64}$' && \
    git cat-file -e "$remote_base^{commit}" 2>/dev/null && \
    git merge-base --is-ancestor "$remote_base" "$REMOTE_UPDATE_SHA" 2>/dev/null || \
    fail "origin/$UPDATE_BRANCH does not have valid activation ancestry; refusing to overwrite it"

  if [ -z "$PR_NUMBER" ]; then
    if [ -n "$remote_claim" ] && [ "$remote_revision" = "$UPSTREAM_SHA" ]; then
      stop_for_pr "origin/$UPDATE_BRANCH is claimed for package revision $UPSTREAM_SHA; its PR is still being created"
    fi
    fail "activation-owned origin/$UPDATE_BRANCH has no open refresh PR; resolve or delete that exact generated branch before retrying"
  fi
  [ "$PR_HEAD" = "$REMOTE_UPDATE_SHA" ] && \
    [ "$PR_AGENTS" = "agent:$BOOTSTRAP_AGENT" ] && \
    [ "$PR_OWNER_BODY" = "true" ] && [ "$PR_ISSUELESS" = "true" ] || \
    fail "the open refresh PR does not prove sole package-bootstrap ownership of origin/$UPDATE_BRANCH"

  update_tree=$(git rev-parse "$REMOTE_UPDATE_SHA:$PREFIX" 2>/dev/null || true)
  if [ "$update_tree" = "$UPSTREAM_TREE" ]; then
    [ -n "$PR_NUMBER" ] || \
      fail "origin/$UPDATE_BRANCH contains the current package but has no open PR"
    if [ "$PR_DRAFT" = "true" ]; then
      stop_for_pr "package refresh PR #$PR_NUMBER is in progress at $PR_URL"
    fi
    stop_for_pr "package refresh PR #$PR_NUMBER is ready at $PR_URL"
  fi
  if [ -n "$remote_claim" ] && [ "$remote_revision" = "$UPSTREAM_SHA" ]; then
    stop_for_pr "package refresh PR #$PR_NUMBER is in progress at $PR_URL"
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
WORKTREE_ADDED=0

cleanup() {
  cleanup_status=$?
  trap - EXIT HUP INT TERM
  if [ "$WORKTREE_ADDED" -eq 1 ]; then
    git -C "$ROOT" worktree remove --force "$TEMP_WORKTREE" >/dev/null 2>&1 || true
  fi
  [ ! -f "$BODY_FILE" ] || rm -f -- "$BODY_FILE"
  rmdir "$TEMP_PARENT" >/dev/null 2>&1 || true
  exit "$cleanup_status"
}
trap cleanup EXIT HUP INT TERM

git worktree add --quiet --detach "$TEMP_WORKTREE" "origin/$BASE" || \
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

resolve_pr_by_head() {
  resolve_attempt=0
  while [ "$resolve_attempt" -lt 5 ]; do
    resolved_pr=$(gh pr view "$UPDATE_BRANCH" --repo "$REPO" --json number,url \
      --jq '[.number, .url] | @tsv' 2>/dev/null) && {
      printf '%s\n' "$resolved_pr"
      return 0
    }
    resolve_attempt=$((resolve_attempt + 1))
    sleep 1
  done
  return 1
}

claim_message | git -C "$TEMP_WORKTREE" commit --allow-empty --quiet --file=- || \
  fail "cannot create the package refresh claim commit"
claim_sha=$(git -C "$TEMP_WORKTREE" rev-parse HEAD)
if ! git -C "$TEMP_WORKTREE" push --quiet \
  --force-with-lease="refs/heads/$UPDATE_BRANCH:$REMOTE_UPDATE_SHA" \
  origin "$claim_sha:refs/heads/$UPDATE_BRANCH"; then
  observed_lines=$(git ls-remote --heads origin "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
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

if [ -n "$PR_NUMBER" ] && [ "$PR_DRAFT" != "true" ]; then
  gh pr ready "$PR_NUMBER" --repo "$REPO" --undo >/dev/null 2>&1 || \
    fail "claimed the refresh branch but could not return PR #$PR_NUMBER to draft"
  PR_DRAFT=true
fi
write_pending_body
if [ -z "$PR_NUMBER" ]; then
  created_pr=$(gh pr create --repo "$REPO" --base "$BASE" --head "$UPDATE_BRANCH" \
    --draft --title "Refresh mounted AI Team package to ${UPSTREAM_SHA:0:12}" \
    --body-file "$BODY_FILE" --label "agent:$BOOTSTRAP_AGENT" --label task:active \
    2>/dev/null) || true
  resolved_pr=$(resolve_pr_by_head) || \
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

# The fixed remote branch is now the activation lock. Scan again so a claim
# that became visible between the first scan and this atomic lock cannot be
# updated underneath.
active_lanes=$(scan_active_lanes) || \
  fail "refresh lock is established, but active lanes could not be rechecked"
[ -z "$active_lanes" ] || \
  fail "refresh lock is established, but a task lane appeared concurrently: $(printf '%s' "$active_lanes" | tr '\n' ' ')"

# Always reconstruct from the exact current default branch. An existing refresh
# PR is generated state, so replacing its obsolete tree under a lease cannot
# discard task-owned work.
if git -C "$TEMP_WORKTREE" rev-parse "HEAD:$PREFIX" >/dev/null 2>&1; then
  if ! git -C "$TEMP_WORKTREE" subtree pull --prefix="$PREFIX" "$SOURCE" "$UPSTREAM_SHA" --squash; then
    fail "git subtree pull conflicted; origin/$UPDATE_BRANCH remains draft and the caller checkout is unchanged"
  fi
else
  if ! git -C "$TEMP_WORKTREE" subtree add --prefix="$PREFIX" "$SOURCE" "$UPSTREAM_SHA" --squash; then
    fail "git subtree add failed; origin/$UPDATE_BRANCH remains draft and the caller checkout is unchanged"
  fi
fi

result_tree=$(git -C "$TEMP_WORKTREE" rev-parse "HEAD:$PREFIX" 2>/dev/null || true)
[ "$result_tree" = "$UPSTREAM_TREE" ] || \
  fail "subtree result does not exactly match package revision $UPSTREAM_SHA"

if ! run_mounted_suite "$TEMP_WORKTREE"; then
  fail "mounted mechanism tests failed; origin/$UPDATE_BRANCH remains draft and the caller checkout is unchanged"
fi

metadata_message | git -C "$TEMP_WORKTREE" commit --allow-empty --quiet --file=- || \
  fail "cannot record the verified package revision"
FINAL_SHA=$(git -C "$TEMP_WORKTREE" rev-parse HEAD)
if ! git -C "$TEMP_WORKTREE" push --quiet \
  --force-with-lease="refs/heads/$UPDATE_BRANCH:$lease_sha" \
  origin "$FINAL_SHA:refs/heads/$UPDATE_BRANCH"; then
  observed_lines=$(git ls-remote --heads origin "refs/heads/$UPDATE_BRANCH" 2>/dev/null) || \
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

write_verified_body
if [ -z "$PR_NUMBER" ]; then
  created_pr=$(gh pr create --repo "$REPO" --base "$BASE" --head "$UPDATE_BRANCH" \
    --draft --title "Refresh mounted AI Team package to ${UPSTREAM_SHA:0:12}" \
    --body-file "$BODY_FILE" --label "agent:$BOOTSTRAP_AGENT" --label task:active \
    2>/dev/null) || true
  resolved_pr=$(resolve_pr_by_head) || \
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
gh pr edit "$PR_NUMBER" --repo "$REPO" --remove-label task:active >/dev/null 2>&1 || true
gh pr ready "$PR_NUMBER" --repo "$REPO" >/dev/null 2>&1 || \
  fail "published the verified refresh but could not mark PR #$PR_NUMBER ready"

final_pr_state=$(gh pr view "$PR_NUMBER" --repo "$REPO" \
  --json headRefOid,isDraft,labels \
  --jq '[.headRefOid, .isDraft, ([.labels[].name | select(startswith("agent:"))] | sort | join(",")), ([.labels[].name] | any(. == "task:active")), ([.labels[].name] | any(. == "task:review"))] | @tsv' \
  2>/dev/null) || fail "cannot verify the final state of PR #$PR_NUMBER"
IFS=$'\t' read -r final_pr_head final_pr_draft final_pr_agents final_pr_active final_pr_review <<EOF
$final_pr_state
EOF
[ "$final_pr_head" = "$FINAL_SHA" ] && [ "$final_pr_draft" = "false" ] && \
  [ "$final_pr_agents" = "agent:$BOOTSTRAP_AGENT" ] && \
  [ "$final_pr_active" = "false" ] && [ "$final_pr_review" = "true" ] || \
  fail "PR #$PR_NUMBER did not reach the exact package-bootstrap/task:review state; onboarding remains paused"

stop_for_pr "package revision $UPSTREAM_SHA passed the mounted suite in PR #$PR_NUMBER at $PR_URL"
