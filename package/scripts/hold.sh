#!/usr/bin/env bash
#
# Set or clear a *named* review hold — one label per holding reviewer, so two
# reviewers with independent open findings on the same PR never collapse into
# one anonymous `hold:review` boolean that either can remove (#385).
#
#   CLAIM_AGENT=<stable-agent-id> docs/ai-team/scripts/hold.sh review add <pr-number>
#   CLAIM_AGENT=<stable-agent-id> docs/ai-team/scripts/hold.sh review clear <pr-number>
#
# `add` creates the PR-scoped label `hold:review:<agent>` (creating the label
# itself on first use, the same lazy pattern claim.sh uses for `agent:<id>`)
# and applies it. Plain `clear` (no --steward) removes only CLAIM_AGENT's own
# label — never another holder's.
#
# Steward transfer of an unresponsive holder's hold — the one case where a
# third party may clear a hold that isn't theirs — requires the target agent,
# an explicit verifiable-finding classification, and a recorded reason, all
# mandatory together, never inferred:
#
#   CLAIM_AGENT=<steward-id> docs/ai-team/scripts/hold.sh review clear <pr-number> \
#     --steward <holder-agent> --discharge verifiable \
#     --reason "<what was discharged, and how you know>"
#
# `--discharge judgment` is deliberately refused: a steward can verify an
# observable fact on behalf of an unresponsive holder, but cannot silently
# substitute their own judgment for the reviewer's. The named hold stays set
# until its holder accepts the trade or otherwise records a superseding verdict.
#
# This clears exactly hold:review:<holder-agent> — no other holder's label is
# touched — and posts the reason as a headered PR comment, so "I named the
# absent holder in a comment" (once unread prose) becomes durable, attributed
# evidence the gate's history and any later reader can see. Skipping this path
# for a bare `gh pr edit --remove-label` is exactly the failure this script
# exists to prevent: the tool becomes the thing you route around at the one
# moment attribution matters most.
#
# `--steward` names who is acting and checks no role of its own — any agent can
# pass it. `--discharge verifiable` and the recorded --reason make the claim
# auditable: the script cannot prove the classification, but it can prevent the
# ambiguity from disappearing into an untyped status comment. Do not mistake
# the flag for a permission check.
#
# A pre-#385 unattributed bare `hold:review` label has no owner to name, so
# it clears through a separate, explicit path rather than by accident:
#
#   CLAIM_AGENT=<agent-id> docs/ai-team/scripts/hold.sh review clear <pr-number> \
#     --legacy --reason "<what was discharged, and how you know>"
#
# hold:author is unaffected — it names the PR's one author lane already, so it
# has no multi-holder ambiguity to fix.

set -euo pipefail

kind="${1:-}"
action="${2:-}"
pr="${3:-}"
shift 3 2>/dev/null || true
agent="${CLAIM_AGENT:-}"

usage() {
  cat >&2 <<'EOF'
usage:
  CLAIM_AGENT=<stable-agent-id> hold.sh review add   <pr-number>
  CLAIM_AGENT=<stable-agent-id> hold.sh review clear  <pr-number>
  CLAIM_AGENT=<steward-id>      hold.sh review clear  <pr-number> --steward <holder-agent> --discharge verifiable --reason "<evidence>"
  CLAIM_AGENT=<agent-id>        hold.sh review clear  <pr-number> --legacy --reason "<evidence>"
EOF
  exit 2
}

[[ "$kind" == "review" ]] || usage
[[ "$action" == "add" || "$action" == "clear" ]] || usage
[[ "$pr" =~ ^[0-9]+$ ]] || usage

steward_target=""
discharge=""
reason=""
legacy=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --steward) steward_target="${2:-}"; shift 2 ;;
    --discharge) discharge="${2:-}"; shift 2 ;;
    --reason)  reason="${2:-}"; shift 2 ;;
    --legacy)  legacy=1; shift ;;
    *) usage ;;
  esac
done

if [[ -n "$steward_target" || -n "$discharge" || -n "$reason" || -n "$legacy" ]]; then
  [[ "$action" == "clear" ]] || { echo "--steward/--discharge/--reason/--legacy only apply to clear" >&2; usage; }
fi

if [[ -n "$legacy" && ( -n "$steward_target" || -n "$discharge" ) ]]; then
  echo "refusing: --legacy and --steward/--discharge are mutually exclusive — a bare hold has no owner to name" >&2
  exit 2
fi

if [[ -n "$legacy" ]]; then
  [[ -n "$reason" ]] || {
    echo "--legacy requires --reason — an unattributed hold has no holder to hold the omission against, so the evidence is the only record there is" >&2
    exit 2
  }
elif [[ -n "$steward_target" || -n "$discharge" || -n "$reason" ]]; then
  [[ -n "$steward_target" && -n "$discharge" && -n "$reason" ]] || {
    echo "--steward, --discharge verifiable, and --reason must all be given — an unclassified steward transfer is exactly the ambiguity the record must preserve" >&2
    exit 2
  }
  [[ "$discharge" == "verifiable" || "$discharge" == "judgment" ]] || {
    echo "--discharge must be verifiable or judgment" >&2
    exit 2
  }
fi

if [[ ! "$agent" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
  echo "CLAIM_AGENT must be a lower-case stable agent id (without agent:)" >&2
  exit 2
fi

if [[ -n "$steward_target" ]]; then
  if [[ ! "$steward_target" =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
    echo "--steward must be a lower-case stable agent id (without agent:)" >&2
    exit 2
  fi
  if [[ "$steward_target" == "$agent" ]]; then
    echo "refusing: --steward $steward_target is your own id — clear your own hold without --steward" >&2
    exit 2
  fi
fi

if [[ "$discharge" == "judgment" ]]; then
  echo "refusing steward clearance of $steward_target's judgment finding — only the holder can accept that trade; leave hold:review:$steward_target set and obtain their recorded verdict" >&2
  exit 1
fi

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) || {
  echo "cannot resolve the repository from gh" >&2
  exit 2
}

pr_json=$(gh pr view "$pr" --repo "$repo" --json number,state 2>/dev/null) || {
  echo "cannot read PR #$pr from $repo" >&2
  exit 2
}

state=$(jq -r .state <<<"$pr_json")
if [[ "$state" != "OPEN" ]]; then
  echo "refusing #$pr: state is $state" >&2
  exit 1
fi

if [[ -n "$legacy" ]]; then
  hold_label="hold:review"
else
  holder="${steward_target:-$agent}"
  hold_label="hold:review:$holder"
fi

label_present() {
  # A separate lookup, not trust in gh pr edit's exit code: --remove-label
  # exits 0 whether or not the label was ever present, and can silently
  # no-op on a partial failure. Interpolated directly rather than via a jq
  # --arg: $hold_label is built only from already-regex-validated agent ids
  # or the literal string "hold:review", so it can never carry a quote or
  # break out of the jq string literal (#420 review, P1).
  #
  # Three states, not two: a failed lookup prints "unknown", never "false" —
  # `|| true` swallowing the gh error used to make an API failure read as a
  # negative result, which the post-removal check then trusted as "gone"
  # (#420 review, second pass). "unknown" is neither name a caller treats as
  # confirmation; the pre-check (which wants "true") and the post-check
  # (which wants exactly "false") both correctly refuse on it.
  local out
  out=$(gh pr view "$pr" --repo "$repo" --json labels \
    --jq ".labels | any(.name == \"$hold_label\")" 2>/dev/null) || { echo "unknown"; return; }
  [[ "$out" == "true" || "$out" == "false" ]] || out="unknown"
  printf '%s' "$out"
}

if [[ "$action" == "add" ]]; then
  # Labels on live Issues and PRs are the identity registry (see claim.sh) —
  # create this holder's label only after validation, before it's applied.
  # `$label` is a jq keyword (label $out | break $out) and a parse error as a
  # jq variable name (#403) — named `$want` here for the same reason.
  labels=$(gh label list --repo "$repo" --limit 1000 --json name 2>/dev/null) || {
    echo "cannot read labels from $repo" >&2
    exit 2
  }
  if ! jq -e --arg want "$hold_label" 'any(.name == $want)' <<<"$labels" >/dev/null; then
    gh label create "$hold_label" --repo "$repo" --color "b60205" \
      --description "AI-team review hold: open finding from $holder — that agent clears it"
  fi
  gh pr edit "$pr" --repo "$repo" --add-label "$hold_label" >/dev/null
  echo "set $hold_label on PR #$pr"
else
  evidenced=""
  [[ -n "$steward_target" || -n "$legacy" ]] && evidenced=1

  # Evidence before mutation, not after (#420 review, P2): if posting the
  # comment fails, nothing has changed yet, so the worst case is a no-op —
  # never a hold silently cleared with its evidence lost to a transient
  # failure. Only the plain self-clear path (no evidence required) skips
  # straight to removal.
  if [[ -n "$evidenced" ]]; then
    # An evidenced clear asserts "this specific hold existed and I am
    # closing it out" — if the label was never set (most often a typo'd
    # --steward id), that assertion is false, and posting the comment
    # anyway would manufacture evidence for a non-event exactly like a
    # failed removal would (#420 review P1). Check before acting.
    if [[ "$(label_present)" != "true" ]]; then
      echo "refusing #$pr: $hold_label is not currently set — nothing to clear (check the holder id for a typo)" >&2
      exit 1
    fi

    comment_file=$(mktemp)
    trap 'rm -f "$comment_file"' EXIT
    {
      printf '**From:** %s\n\n' "$agent"
      printf '**Type:** status\n\n'
      if [[ -n "$legacy" ]]; then
        printf 'Clearing the unattributed legacy %s label.\n\n' "$hold_label"
      else
        printf 'Steward-clearing %s — %s clearing on their behalf as an unresponsive holder.\n\n' \
          "$hold_label" "$steward_target"
        printf 'Discharge classification: %s\n\n' "$discharge"
      fi
      printf 'Discharge evidence: %s\n' "$reason"
    } >"$comment_file"
    if ! gh pr comment "$pr" --repo "$repo" --body-file "$comment_file" >/dev/null; then
      echo "refusing #$pr: could not post the evidence comment — nothing changed" >&2
      exit 2
    fi
  fi

  gh pr edit "$pr" --repo "$repo" --remove-label "$hold_label" >/dev/null 2>&1 || true

  after="$(label_present)"
  if [[ "$after" != "false" ]]; then
    if [[ "$after" == "unknown" ]]; then
      echo "could not confirm $hold_label was actually removed from #$pr — the verification lookup itself failed, so this is not reported as cleared" >&2
    else
      echo "$hold_label is still present on #$pr after attempting to remove it — the label was not actually cleared" >&2
    fi
    if [[ -n "$evidenced" ]]; then
      echo "the evidence comment above is already posted and stands as the record of this attempt — retry the clear once the label removal itself succeeds and can be confirmed" >&2
    fi
    exit 1
  fi

  if [[ -n "$legacy" ]]; then
    echo "cleared legacy $hold_label on PR #$pr (reason recorded on the PR)"
  elif [[ -n "$steward_target" ]]; then
    echo "steward-cleared $hold_label on PR #$pr ($discharge discharge and reason recorded on the PR)"
  else
    echo "cleared $hold_label on PR #$pr"
  fi
fi
