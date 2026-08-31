#!/usr/bin/env bash
# decide.sh — autonomous deliberation and voting on the owning issue (#430).
#
# Replaces "stop and ask the owner" for product/architecture choices with one
# machine-readable flow: propose, vote, close. State lives entirely in
# structured comments (board.sh's **From:**/**Type:** header plus this
# script's own **Decision:**/**Option:**/... fields) — no new labels, so
# gate.sh, orient.sh, and board.sh's existing comment-scanning idioms all
# apply unchanged.
#
#   CLAIM_AGENT=<id> decide.sh propose <issue> --id <decision-id> \
#     --question "<question>" --options "optA,optB,optC" --recommend optA \
#     [--deadline-minutes N] [evidence/trade-off body…]
#
#   CLAIM_AGENT=<id> decide.sh vote <issue> --id <decision-id> --option optA \
#     <rationale, tied to the authority stack…>
#
#   CLAIM_AGENT=<id> decide.sh notify <issue> --id <decision-id> \
#     --acknowledged agentA,agentB
#
#   CLAIM_AGENT=<id> decide.sh close <issue> --id <decision-id> \
#     [--decision <option> --rationale "<tie-break/available-tally reasoning>" \
#      --authority-effect none|self [--owner-delegation "<durable link>"]] \
#     [--owner <agent>] [--revisit-if "<condition that would reopen this>"]
#
#   decide.sh status <issue> [--id <decision-id>]
#
# Quorum: the proposal's immutable **Notify:** roster defines the voters for
# its entire round. With 3+ snapshotted agents, 3 distinct attributable votes
# are quorum. With fewer, every snapshotted agent must vote. A currently active
# agent still must close the round, but landing a lane mid-round does not erase
# a vote from someone who was active when the proposal opened (#33). A clear
# majority among quorum-reached votes closes on its own; a tie, or a closed
# deadline without quorum, requires the closer to pass
# --decision/--rationale/--authority-effect explicitly — this script never
# guesses a tie-break, it only refuses to let the round stall. A closer who
# declares --authority-effect self (this round would expand, waive, or
# transfer the closer's own authority) is refused outright unless an explicit
# --owner-delegation <durable link> names the owner's specific, named delegation
# of this exact permission — silence is never delegation, and it applies to
# this one close only. The carve-out is enforced on the record, not left to the
# closer's memory (#436 review).
#
# propose() snapshots the active roster as **Notify:**, and the decision
# record separates three honestly-named facts: who voted without appearing in
# the immutable proposal snapshot (**Filtered:**, excluded from that round's
# quorum and tally), who never cast a vote (**Did-Not-Vote:**, which says
# nothing about whether they saw the round — an abstention looks identical to a
# miss), and who neither voted nor was ever explicitly recorded via
# `notify --acknowledged` as having received it (**Unacknowledged:**, a
# fail-closed caller-supplied record, since decide.sh cannot itself deliver a
# message — only the invoking agent's own cross-session messaging can).
#
# What this cannot do: repeal an explicit owner prohibition, a repository
# safety rule, review independence, a live hold, or a missing external
# credential/permission (short of the one scoped, explicit, linked
# delegation above). Those are recorded as a recommendation and a request
# for the specific missing authority, never voted around (see
# docs/ai-team/README.md, "Autonomous deliberation").

set -uo pipefail

DECIDE_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$DECIDE_DIR/_default_branch.sh"
REPO="${DECIDE_REPO:-${BOARD_REPO:-$(ai_team_origin_repo 2>/dev/null || true)}}"
[ -n "$REPO" ] || REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)
if [ -z "$REPO" ]; then
  echo "decide.sh: cannot determine the repository. Set DECIDE_REPO or BOARD_REPO," >&2
  echo "or run inside a checkout whose origin remote points at a GitHub repository." >&2
  exit 2
fi
BOARD_SH=""$(cd "${BASH_SOURCE[0]%/*}" && pwd)"/board.sh"
MAX_DEADLINE_MINUTES=30
# Reserved for "this field does not apply on this record's path" — never a
# value real user content is allowed to equal (#436 review, terra P1,
# sixth round: an ordinary word like "none" collides with genuine content
# whose actual reasoning happens to be that word; a closer's real
# --rationale is refused outright if it literally equals this).
NOT_APPLICABLE="(not applicable)"

AGENT_RE='^[a-z0-9]+([._-][a-z0-9]+)*$'
DECISION_ID_RE='^[a-z0-9]+(-[a-z0-9]+)*$'
OPTION_RE='^[A-Za-z0-9][A-Za-z0-9 _.-]*$'

usage() {
  sed -n '2,57p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

command="${1:-}"
[ -n "$command" ] || usage
shift

agent="${CLAIM_AGENT:-}"
require_agent() {
  if [[ ! "$agent" =~ $AGENT_RE ]]; then
    echo "CLAIM_AGENT must be a lower-case stable agent id (without agent:)" >&2
    exit 2
  fi
}

require_decision_id() {
  local id="$1"
  if [[ ! "$id" =~ $DECISION_ID_RE ]]; then
    echo "--id must be lower-case kebab-case (e.g. locale-fallback-order): got '$id'" >&2
    exit 2
  fi
}

# `[ -n "$x" ]` tests for a non-empty string, not non-empty content —
# whitespace passes it (#436 review, terra P2: `[ -n "   " ]` is true, so
# whitespace-only evidence/rationale satisfied the "required" check).
is_blank() {
  [[ "$1" =~ ^[[:space:]]*$ ]]
}

resolve_repo() {
  gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || printf '%s' "$REPO"
}

# Every agent with a live lane: an open PR or an open agent:*-labelled issue.
# Same roster board.sh's hygiene pass already scans (#385's active-lanes
# definition) — reused here rather than invented separately, so "active" has
# exactly one meaning across the charter.
active_agents() {
  local repo="$1"
  # tr -d '\r': native-Windows gh/jq output can retain a trailing CR (#436
  # review, terra's P1 — 3/33 focused-suite failures on native Windows, the
  # saved roster losing/mangling identities and falsely marking real voters
  # not-reached). Every consumer of this list — quorum, tally filtering,
  # Notify/Did-Not-Vote — depends on exact string equality against agent ids
  # parsed elsewhere, so one un-stripped \r silently breaks every one of them.
  {
    gh pr list --repo "$repo" --state open --limit 100 --json labels \
      --jq '.[].labels[].name | select(startswith("agent:")) | ltrimstr("agent:")' 2>/dev/null
    gh issue list --repo "$repo" --state open --limit 100 --json labels \
      --jq '.[].labels[].name | select(startswith("agent:")) | ltrimstr("agent:")' 2>/dev/null
  } | tr -d '\r' | sort -u
}

# #33's quorum rule as one number: 3 once the immutable proposal snapshot
# reaches 3, otherwise every snapshotted agent. Shared by close() (which
# decides) and status() (which must report the identical requirement, not a
# redraft of it — #436 review, terra P4).
quorum_required_for() {
  local snapshot_count="$1"
  if [ "$snapshot_count" -ge 3 ]; then
    printf '3'
  else
    printf '%s' "$snapshot_count"
  fi
}

fetch_comments() {
  local repo="$1" issue="$2"
  gh issue view "$issue" --repo "$repo" --json comments 2>/dev/null
}

# Filters $comments (issue-view JSON) down to the structured decide.sh
# entries: exactly one **From:** agent, a **Type:** of proposal/vote/decision,
# and a **Decision:** id — reusing gate.sh's "collect every capture, accept
# only an unambiguous single match" idiom for both From and Decision so a
# malformed header excludes a post from the tally instead of corrupting it.
DECIDE_JQ_COMMON='
  # capture() on a non-matching line produces no output at all, not null — so
  # a bare "capture(...).v // fallback" inside split("\n")[] applies the
  # fallback PER LINE, turning one field into one output per line of the
  # body (mostly the fallback) instead of a single value. Collecting into an
  # array first, the way from_agent always did, is the only safe form; every
  # field extractor here goes through it.
  # Trimmed at the shared extraction point, not per-field at each call
  # site: `(?<v>.+)$` captures whatever text follows a field header,
  # including a run of nothing but spaces, so a forged "**Tie-Break:**   "
  # (or any other free-text field) captured a non-empty, non-sentinel
  # value that satisfied every "!= ...\"\"..." presence check downstream
  # while carrying no real content (#436 review, terra P1, seventh round —
  # close() already refuses to *write* whitespace-only content via
  # is_blank(), but a forged record could still be *read* as satisfying
  # it; opus-5 named the general form: every field the writer guards, the
  # reader must guard identically, and trimming here closes it for every
  # field this def feeds, not only the one instance found).
  def one_capture_raw(pattern):
    [((.body // "") | split("\n")[] | capture(pattern; "i") | .v
       | gsub("^\\s+"; "") | gsub("\\s+$"; ""))]
    | unique
    | if length == 1 then .[0] else "" end;
  def one_capture(pattern): one_capture_raw(pattern) | ascii_downcase;
  def from_agent: one_capture("^\\*\\*From:\\*\\*[[:space:]]*(?<v>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)");
  def decide_type: one_capture("^\\*\\*Type:\\*\\*[[:space:]]*(?<v>[a-z]+)[[:space:]]*$");
  def decision_id: one_capture("^\\*\\*Decision:\\*\\*[[:space:]]*(?<v>[a-z0-9][a-z0-9-]*)[[:space:]]*$");
  # Unambiguous From plus a matching Decision id — callers add their own
  # decide_type == "proposal"/"vote"/"decision" check, since the type and the
  # id are independent facts about the same comment.
  def structured($id):
    from_agent != "" and decision_id == $id;
  def chosen_value: one_capture_raw("^\\*\\*Chosen:\\*\\*[[:space:]]*(?<v>.+)$");
  def deciding_agent_value: one_capture_raw("^\\*\\*Deciding-Agent:\\*\\*[[:space:]]*(?<v>.+)$");
  def tally_value: one_capture_raw("^\\*\\*Tally:\\*\\*[[:space:]]*(?<v>.+)$");
  def quorum_value: one_capture_raw("^\\*\\*Quorum:\\*\\*[[:space:]]*(?<v>.+)$");
  def implementation_owner_value: one_capture_raw("^\\*\\*Implementation-Owner:\\*\\*[[:space:]]*(?<v>.+)$");
  def revisit_value: one_capture_raw("^\\*\\*Revisit-If:\\*\\*[[:space:]]*(?<v>.+)$");
  def tie_break_value: one_capture_raw("^\\*\\*Tie-Break:\\*\\*[[:space:]]*(?<v>.+)$");
  def authority_effect_value: one_capture_raw("^\\*\\*Authority-Effect:\\*\\*[[:space:]]*(?<v>.+)$");
  def owner_delegation_value: one_capture_raw("^\\*\\*Owner-Delegation:\\*\\*[[:space:]]*(?<v>.+)$");
  def did_not_vote_value: one_capture_raw("^\\*\\*Did-Not-Vote:\\*\\*[[:space:]]*(?<v>.+)$");
  def unacknowledged_value: one_capture_raw("^\\*\\*Unacknowledged:\\*\\*[[:space:]]*(?<v>.+)$");
  def resolution_value: one_capture("^\\*\\*Resolution:\\*\\*[[:space:]]*(?<v>majority|tie|expired)[[:space:]]*$");
  # A comment only terminates a round if it is an actually well-formed
  # decision record. Third round on this class of gap (#436 review):
  # requiring only Chosen let a four-field forgery close a round (terra
  # P2); requiring Chosen plus five more fields still left five fields —
  # Tie-Break, Authority-Effect, Owner-Delegation, Did-Not-Vote,
  # Unacknowledged — unchecked, two of which (Tie-Break, Authority-Effect)
  # exist specifically to constrain a self-interested closer on the
  # needs_rationale path, so a forged record omitting them defeated that
  # guard through the terminal comment rather than the closing command
  # (opus-5, checking systematically rather than confirming one instance).
  # close() now writes every one of these fields on every record — "none"
  # standing in for a conditional value that does not apply, never omitted
  # — so there is exactly one record shape and requiring "every field
  # present" is complete by construction, not a second hand-maintained
  # list that can drift from what close() writes a third time.
  #
  # Presence alone is not enough (terra P1, fourth round): a forged record
  # can carry every field non-empty while its VALUES contradict each other
  # — Resolution: tie with Authority-Effect: none and Tie-Break: none
  # (claiming a tie-break round happened with nobody declaring anything),
  # or Authority-Effect: self with Owner-Delegation: none (claiming the
  # self-interest override fired with no delegation cited). Requiring the
  # field names alone does not preserve the authority constraint those
  # fields exist to prove — the values close() actually writes together on
  # each branch are checked here as one consistent combination, not merely
  # as five independently-non-empty strings.
  def durable_link: (owner_delegation_value | test("https?://")) or (owner_delegation_value | test("#[0-9]"));
  # Decision eligibility is a proposal-time fact, not a live-board fact.
  # The proposal Notify snapshot is immutable once posted; the active-lane
  # roster is not. Rechecking a terminal record against the current roster made
  # an honestly closed round reopen as soon as the deciding agent lane
  # ended (#443). Callers therefore pass the proposal snapshot as $eligible.
  # close() still checks the live roster before writing, while this shared
  # reader contract proves the author was eligible for this round without
  # letting later lane churn rewrite history.
  def valid_decision($opts; $eligible; $na):
    decide_type == "decision"
    and (chosen_value as $c | $opts | index($c)) != null
    and deciding_agent_value != ""
    and deciding_agent_value == from_agent
    and resolution_value != ""
    and tally_value != ""
    and quorum_value != ""
    and implementation_owner_value != ""
    and revisit_value != ""
    and tie_break_value != ""
    and authority_effect_value != ""
    and owner_delegation_value != ""
    and did_not_vote_value != ""
    and unacknowledged_value != ""
    and (from_agent as $a | $eligible | index($a)) != null
    and (
      if resolution_value == "majority" then
        tie_break_value == $na and authority_effect_value == "none" and owner_delegation_value == "none"
      else
        tie_break_value != $na
        and (authority_effect_value == "none" or authority_effect_value == "self")
        and (if authority_effect_value == "self" then durable_link else owner_delegation_value == "none" end)
      end
    );
'

propose() {
  local issue="${1:-}"; shift || true
  [[ "$issue" =~ ^[0-9]+$ ]] || { echo "propose: issue number required" >&2; exit 2; }

  local id="" question="" options_csv="" recommend="" deadline_minutes="$MAX_DEADLINE_MINUTES"
  local body=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --id) id="${2:-}"; shift 2 ;;
      --question) question="${2:-}"; shift 2 ;;
      --options) options_csv="${2:-}"; shift 2 ;;
      --recommend) recommend="${2:-}"; shift 2 ;;
      --deadline-minutes) deadline_minutes="${2:-}"; shift 2 ;;
      *) body="${body:+$body }$1"; shift ;;
    esac
  done

  require_agent
  [ -n "$id" ] || { echo "propose: --id required" >&2; exit 2; }
  require_decision_id "$id"
  [ -n "$question" ] || { echo "propose: --question required" >&2; exit 2; }
  [ -n "$options_csv" ] || { echo "propose: --options required (comma-separated)" >&2; exit 2; }
  [ -n "$recommend" ] || { echo "propose: --recommend required" >&2; exit 2; }
  # #430 requires evidence/trade-offs on the proposal, not just a bare
  # question — fail closed rather than accept a blank body (#436 review,
  # terra P4, and whitespace-only closed by terra P2).
  is_blank "$body" && {
    echo "propose: evidence is required — pass trade-offs, costs, risks, reversibility, and what a wrong answer would break as trailing text; a bare or whitespace-only question is not a proposal" >&2
    exit 2
  }
  [[ "$deadline_minutes" =~ ^[0-9]+$ ]] && [ "$deadline_minutes" -ge 1 ] && [ "$deadline_minutes" -le "$MAX_DEADLINE_MINUTES" ] || {
    echo "propose: --deadline-minutes must be 1..$MAX_DEADLINE_MINUTES (one heartbeat)" >&2
    exit 2
  }

  local -a options=()
  local opt trimmed
  IFS=',' read -ra options <<<"$options_csv"
  local seen="" clean_options=()
  for opt in "${options[@]}"; do
    trimmed="${opt#"${opt%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [ -n "$trimmed" ] || { echo "propose: empty option in --options" >&2; exit 2; }
    [[ "$trimmed" =~ $OPTION_RE ]] || { echo "propose: option '$trimmed' has characters outside [A-Za-z0-9 _.-] (commas separate options, so they cannot appear inside one)" >&2; exit 2; }
    case ",$seen," in
      *",$trimmed,"*) echo "propose: duplicate option '$trimmed'" >&2; exit 2 ;;
    esac
    seen="${seen:+$seen,}$trimmed"
    clean_options+=("$trimmed")
  done
  [ "${#clean_options[@]}" -ge 2 ] || { echo "propose: need at least 2 distinct options" >&2; exit 2; }
  case ",$seen," in
    *",$recommend,"*) ;;
    *) echo "propose: --recommend '$recommend' is not one of the declared --options" >&2; exit 2 ;;
  esac

  local repo; repo=$(resolve_repo)
  local comments; comments=$(fetch_comments "$repo" "$issue") || { echo "propose: cannot read #$issue from $repo" >&2; exit 2; }

  local existing
  existing=$(printf '%s' "$comments" | jq -r --arg id "$id" "$DECIDE_JQ_COMMON"'
    [.comments[] | select(structured($id) and decide_type == "proposal")] | length' 2>/dev/null || echo 0)
  if [ "${existing:-0}" -gt 0 ]; then
    echo "propose: decision id '$id' already has a proposal on #$issue — decision ids are permanent per issue, pick a new one" >&2
    exit 1
  fi

  local deadline
  deadline=$(date -u -d "+${deadline_minutes} minutes" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null) \
    || deadline=$(date -u -v"+${deadline_minutes}M" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null) \
    || { echo "propose: could not compute the deadline timestamp" >&2; exit 2; }

  # This script cannot itself reach another agent — only the caller's own
  # cross-session messaging can — but it can make the round's expected
  # audience a durable, checkable fact instead of a silent gap. Snapshotting
  # the active roster now and diffing it against who actually voted at
  # close time is what turns "an agent never learned this was decided" from
  # an invisible failure into a recorded one (#436 review, terra's P1 — the
  # concern was raised on #430's spec by opus-5 and missed in the first
  # implementation review).
  local notify_roster; notify_roster=$(active_agents "$repo" | paste -sd, -)

  local payload
  payload=$(printf '**Decision:** %s\n**Options:** %s\n**Recommend:** %s\n**Deadline:** %s\n**Notify:** %s\n\n%s\n\n%s' \
    "$id" "$seen" "$recommend" "$deadline" "$notify_roster" "$question" "$body")

  CLAIM_AGENT="$agent" "$BOARD_SH" post "$issue" --agent "$agent" --type proposal "$payload" || {
    echo "propose: could not post '$id' to #$issue — nothing recorded" >&2
    exit 2
  }
  echo "proposed '$id' on #$issue — options: $seen — deadline $deadline"
  if [ -n "$notify_roster" ]; then
    echo "notify the active roster now (cross-session messaging, or a board post) so this round isn't decided only by whoever happens to read the issue: $notify_roster"
  fi
}

vote() {
  local issue="${1:-}"; shift || true
  [[ "$issue" =~ ^[0-9]+$ ]] || { echo "vote: issue number required" >&2; exit 2; }

  local id="" option="" body=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --id) id="${2:-}"; shift 2 ;;
      --option) option="${2:-}"; shift 2 ;;
      *) body="${body:+$body }$1"; shift ;;
    esac
  done

  require_agent
  [ -n "$id" ] || { echo "vote: --id required" >&2; exit 2; }
  require_decision_id "$id"
  [ -n "$option" ] || { echo "vote: --option required" >&2; exit 2; }
  case "$option" in
    *,*) echo "vote: --option must name exactly one option, not a list" >&2; exit 2 ;;
  esac
  # #430 requires a rationale tied to the authority stack on every vote —
  # fail closed on a blank one (#436 review, terra P4, and whitespace-only
  # closed by terra P2).
  is_blank "$body" && {
    echo "vote: a rationale is required — tie your choice to the authority stack (owner constraints, AGENTS.md, docs/brief.md, the relevant architecture contracts) as trailing text, not whitespace" >&2
    exit 2
  }

  local repo; repo=$(resolve_repo)
  local comments; comments=$(fetch_comments "$repo" "$issue") || { echo "vote: cannot read #$issue from $repo" >&2; exit 2; }

  local proposal
  proposal=$(printf '%s' "$comments" | jq -c --arg id "$id" "$DECIDE_JQ_COMMON"'
    def opts: one_capture_raw("^\\*\\*Options:\\*\\*[[:space:]]*(?<v>.+)$");
    def notify: one_capture_raw("^\\*\\*Notify:\\*\\*[[:space:]]*(?<v>.*)$");
    [.comments[] | select(structured($id) and decide_type == "proposal") | {options: opts, notify: notify, proposer: from_agent, createdAt}]
    | if length == 1 then .[0] else null end' 2>/dev/null)
  if [ -z "$proposal" ] || [ "$proposal" = "null" ]; then
    echo "vote: no open proposal '$id' found on #$issue — check the id, or propose it first" >&2
    exit 1
  fi

  local vote_options_json
  vote_options_json=$(printf '%s' "$proposal" | jq -r '.options' | jq -R 'split(",")')

  local vote_eligible_json
  vote_eligible_json=$(printf '%s' "$proposal" | jq -c '
    (.notify | split(",") | map(select(length > 0))) as $snapshot
    | if ($snapshot | length) > 0 then $snapshot else [.proposer] end')

  local closed
  closed=$(printf '%s' "$comments" | jq -r --arg id "$id" --argjson opts "$vote_options_json" --argjson eligible "$vote_eligible_json" --arg na "$NOT_APPLICABLE" "$DECIDE_JQ_COMMON"'
    [.comments[] | select(structured($id) and valid_decision($opts; $eligible; $na))] | length' 2>/dev/null || echo 0)
  if [ "${closed:-0}" -gt 0 ]; then
    echo "vote: '$id' on #$issue is already closed — this round is over" >&2
    exit 1
  fi

  local declared_options
  declared_options=",$(printf '%s' "$proposal" | jq -r '.options'),"
  case "$declared_options" in
    *",$option,"*) ;;
    *)
      echo "vote: '$option' is not one of this proposal's declared options ($(printf '%s' "$proposal" | jq -r '.options'))" >&2
      exit 2
      ;;
  esac

  # The Notify roster is the round's boundary, not the later live roster:
  # agents who land their lane remain enfranchised, while a newly appearing or
  # long-dead identity cannot enter an already-open round just by posting a
  # structured vote. Preserve the out-of-snapshot vote for audit, but warn
  # that it will be filtered (#33).
  if ! jq -e --arg agent "$agent" 'index($agent) != null' <<<"$vote_eligible_json" >/dev/null; then
    echo "vote: warning: '$agent' is not in this proposal's immutable Notify roster; the vote is recorded but filtered from this round's quorum and tally" >&2
  fi

  local payload
  payload=$(printf '**Decision:** %s\n**Option:** %s\n\n%s' "$id" "$option" "$body")

  CLAIM_AGENT="$agent" "$BOARD_SH" post "$issue" --agent "$agent" --type vote "$payload" || {
    echo "vote: could not post the vote on #$issue — nothing recorded" >&2
    exit 2
  }
  echo "voted '$option' on '$id' (#$issue)"
}

# A caller-supplied, fail-closed delivery record (#436 review, terra P1 #1):
# decide.sh cannot itself deliver a cross-session message, only the calling
# agent's own messaging tool can — so this records the ONE thing the script
# can honestly assert: that the invoking agent is personally attesting these
# specific agents received the round (a successful SendMessage, a reply, a
# board post they're known to have read). Nobody is ever acknowledged by
# silence or by default; only a name explicitly listed here counts.
notify() {
  local issue="${1:-}"; shift || true
  [[ "$issue" =~ ^[0-9]+$ ]] || { echo "notify: issue number required" >&2; exit 2; }

  local id="" acknowledged_csv=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --id) id="${2:-}"; shift 2 ;;
      --acknowledged) acknowledged_csv="${2:-}"; shift 2 ;;
      *) echo "notify: unrecognized argument '$1'" >&2; exit 2 ;;
    esac
  done

  require_agent
  [ -n "$id" ] || { echo "notify: --id required" >&2; exit 2; }
  require_decision_id "$id"
  [ -n "$acknowledged_csv" ] || { echo "notify: --acknowledged <agent-csv> required — who specifically is confirmed reached" >&2; exit 2; }

  local -a ack_ids=()
  local a trimmed
  IFS=',' read -ra ack_ids <<<"$acknowledged_csv"
  local seen=""
  for a in "${ack_ids[@]}"; do
    trimmed="${a#"${a%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ "$trimmed" =~ $AGENT_RE ]] || { echo "notify: '$trimmed' in --acknowledged is not a valid stable agent id" >&2; exit 2; }
    seen="${seen:+$seen,}$trimmed"
  done

  local repo; repo=$(resolve_repo)
  local comments; comments=$(fetch_comments "$repo" "$issue") || { echo "notify: cannot read #$issue from $repo" >&2; exit 2; }

  local proposal_exists
  proposal_exists=$(printf '%s' "$comments" | jq -r --arg id "$id" "$DECIDE_JQ_COMMON"'
    [.comments[] | select(structured($id) and decide_type == "proposal")] | length' 2>/dev/null || echo 0)
  if [ "${proposal_exists:-0}" -eq 0 ]; then
    echo "notify: no proposal '$id' found on #$issue — check the id, or propose it first" >&2
    exit 1
  fi

  local payload
  payload=$(printf '**Decision:** %s\n**Acknowledged:** %s' "$id" "$seen")

  CLAIM_AGENT="$agent" "$BOARD_SH" post "$issue" --agent "$agent" --type acknowledgement "$payload" || {
    echo "notify: could not post the acknowledgement on #$issue — nothing recorded" >&2
    exit 2
  }
  echo "recorded '$seen' as acknowledged for '$id' (#$issue)"
}

# Latest well-formed vote per agent for $id, cast after the proposal, with an
# unambiguous **From:** and exactly one declared **Option:** value. A vote
# with 0 or 2+ **Option:** lines, an unrecognized option, an ambiguous
# **From:**, or a decision id that does not exactly match is excluded here —
# never guessed into the tally.
tally_votes() {
  local comments="$1" id="$2" proposal_created_at="$3" declared_options_json="$4"
  printf '%s' "$comments" | jq -c \
    --arg id "$id" --arg after "$proposal_created_at" --argjson opts "$declared_options_json" \
    "$DECIDE_JQ_COMMON"'
    def one_option:
      [((.body // "") | split("\n")[] | capture("^\\*\\*Option:\\*\\*[[:space:]]*(?<v>.+)$"; "i").v)]
      | unique
      | if length == 1 and (. [0] as $o | $opts | index($o)) != null then .[0] else "" end;
    [.comments[]
     | select(decide_type == "vote" and decision_id == $id and .createdAt > $after)
     | . + {agent: from_agent, option: one_option}
     | select(.agent != "" and .option != "")]
    | sort_by(.agent, .createdAt)
    | group_by(.agent)
    | map(last)
  '
}

close() {
  local issue="${1:-}"; shift || true
  [[ "$issue" =~ ^[0-9]+$ ]] || { echo "close: issue number required" >&2; exit 2; }

  local id="" decision="" rationale="" owner="" revisit="" authority_effect="" owner_delegation=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --id) id="${2:-}"; shift 2 ;;
      --decision) decision="${2:-}"; shift 2 ;;
      --rationale) rationale="${2:-}"; shift 2 ;;
      --owner) owner="${2:-}"; shift 2 ;;
      --revisit-if) revisit="${2:-}"; shift 2 ;;
      --authority-effect) authority_effect="${2:-}"; shift 2 ;;
      --owner-delegation) owner_delegation="${2:-}"; shift 2 ;;
      *) echo "close: unrecognized argument '$1'" >&2; exit 2 ;;
    esac
  done

  if [ -n "$authority_effect" ] && [ "$authority_effect" != "none" ] && [ "$authority_effect" != "self" ]; then
    echo "close: --authority-effect must be 'none' or 'self'" >&2
    exit 2
  fi

  # #430's explicit-delegation clause (terra P3): an owner may delegate one
  # named prohibition, but only explicitly and with a durable link — silence
  # is never delegation, and this never generalizes past this one close.
  # decide.sh cannot verify a claimed delegation's content; what it can and
  # does enforce is structure — a delegation must point at something
  # concrete (a URL or #<issue>), never a bare unsubstantiated assertion —
  # and record it on the decision for anyone to audit afterward.
  if [ -n "$owner_delegation" ]; then
    case "$owner_delegation" in
      *http://*|*https://*|*'#'[0-9]*) ;;
      *)
        echo "close: --owner-delegation must include a durable link (a URL or #<issue>) naming where the owner delegated this — a bare claim is not delegation" >&2
        exit 2
        ;;
    esac
    # terra's #436 P1, sixth round: --authority-effect none --owner-delegation
    # <link> passed here (nothing refused it) and wrote a record
    # valid_decision's own predicate then rejects, since Owner-Delegation is
    # only meaningful — and only permitted to be anything but the
    # not-applicable sentinel — when Authority-Effect is self. Refuse the
    # accepted-but-unclosable input at the source instead of letting it
    # reach the payload.
    if [ "$authority_effect" != "self" ]; then
      echo "close: --owner-delegation only applies when --authority-effect self is also declared — this round does not claim to expand the closer's own authority, so there is nothing to delegate" >&2
      exit 2
    fi
  fi

  require_agent
  [ -n "$id" ] || { echo "close: --id required" >&2; exit 2; }
  require_decision_id "$id"
  owner="${owner:-$agent}"
  if [[ ! "$owner" =~ $AGENT_RE ]]; then
    echo "close: --owner must be a lower-case stable agent id" >&2
    exit 2
  fi

  local repo; repo=$(resolve_repo)
  local comments; comments=$(fetch_comments "$repo" "$issue") || { echo "close: cannot read #$issue from $repo" >&2; exit 2; }

  local proposal
  proposal=$(printf '%s' "$comments" | jq -c --arg id "$id" "$DECIDE_JQ_COMMON"'
    def opts: one_capture_raw("^\\*\\*Options:\\*\\*[[:space:]]*(?<v>.+)$");
    def deadline: one_capture_raw("^\\*\\*Deadline:\\*\\*[[:space:]]*(?<v>.+)$");
    def notify: one_capture_raw("^\\*\\*Notify:\\*\\*[[:space:]]*(?<v>.*)$");
    [.comments[] | select(structured($id) and decide_type == "proposal") | {options: opts, deadline: deadline, notify: notify, createdAt, proposer: from_agent}]
    | if length == 1 then .[0] else null end' 2>/dev/null)
  if [ -z "$proposal" ] || [ "$proposal" = "null" ]; then
    echo "close: no proposal '$id' found on #$issue" >&2
    exit 1
  fi

  local options_csv proposal_created_at deadline_str notify_csv
  options_csv=$(printf '%s' "$proposal" | jq -r '.options')
  proposal_created_at=$(printf '%s' "$proposal" | jq -r '.createdAt')
  deadline_str=$(printf '%s' "$proposal" | jq -r '.deadline')
  notify_csv=$(printf '%s' "$proposal" | jq -r '.notify')
  local options_json; options_json=$(printf '%s\n' "$options_csv" | jq -R 'split(",")')
  local snapshot_json
  snapshot_json=$(printf '%s' "$proposal" | jq -c '
    (.notify | split(",") | map(select(length > 0))) as $snapshot
    | if ($snapshot | length) > 0 then $snapshot else [.proposer] end')

  local roster; roster=$(active_agents "$repo")
  local roster_json; roster_json=$(printf '%s\n' "$roster" | jq -R 'select(length > 0)' | jq -s '.')

  local already_closed
  already_closed=$(printf '%s' "$comments" | jq -r --arg id "$id" --argjson opts "$options_json" --argjson eligible "$snapshot_json" --arg na "$NOT_APPLICABLE" "$DECIDE_JQ_COMMON"'
    [.comments[] | select(structured($id) and valid_decision($opts; $eligible; $na))] | length' 2>/dev/null || echo 0)
  if [ "${already_closed:-0}" -gt 0 ]; then
    echo "close: '$id' on #$issue is already closed" >&2
    exit 1
  fi

  local live_roster_count; live_roster_count=$(printf '%s' "$roster_json" | jq 'length')
  if [ "$live_roster_count" -eq 0 ]; then
    echo "close: no currently active agents found (open PRs or open agent:* issues) — a currently active agent must close this round; check gh connectivity before closing" >&2
    exit 2
  fi
  if ! jq -e --arg agent "$agent" 'index($agent) != null' <<<"$roster_json" >/dev/null; then
    echo "close: '$agent' is not on the current active roster — only an active agent may close this round" >&2
    exit 2
  fi
  if ! jq -e --arg agent "$agent" 'index($agent) != null' <<<"$snapshot_json" >/dev/null; then
    echo "close: '$agent' was not in this proposal's immutable Notify roster — a later lane cannot forge eligibility for an existing round" >&2
    exit 2
  fi

  local all_votes; all_votes=$(tally_votes "$comments" "$id" "$proposal_created_at" "$options_json")

  # Snapshot-filter every vote before any quorum or tally arithmetic touches
  # it. A well-formed **From:** proves identity, not membership in this round:
  # eligibility was fixed when propose() recorded Notify. This keeps a voter
  # who landed their lane after that point, while someone absent from the
  # snapshot — including a long-dead identity — cannot fabricate quorum or
  # shift a majority (#33, preserving #436's fabricated-quorum defense).
  local filtered_voters
  filtered_voters=$(printf '%s' "$all_votes" | jq -r --argjson snapshot "$snapshot_json" '
    [.[] | select(.agent as $a | $snapshot | index($a) == null) | .agent]
    | unique
    | if length == 0 then "none" else map("\(.) (not in snapshot)") | join(", ") end')

  local votes
  votes=$(printf '%s' "$all_votes" | jq -c --argjson snapshot "$snapshot_json" \
    '[.[] | select(.agent as $a | $snapshot | index($a) != null)]')

  local voting_agents_json; voting_agents_json=$(printf '%s' "$votes" | jq -c '[.[].agent] | unique')
  local all_voting_agents_json; all_voting_agents_json=$(printf '%s' "$all_votes" | jq -c '[.[].agent] | unique')
  local voter_count; voter_count=$(printf '%s' "$voting_agents_json" | jq 'length')

  # The roster propose() saw when the round opened, diffed against who
  # actually voted. Named for exactly what this measures — not voting — not
  # for reachability: a deliberate abstention (an agent who read the
  # proposal and chose not to vote) looks identical here to one who never
  # saw it, so this field must never claim to know which (#436 review,
  # terra's P1 #1 — opus-5 initially defended the opposite naming and
  # withdrew that once terra named the gap). Tolerant of a missing/empty
  # Notify field from a malformed or pre-this-fix proposal: the diff is
  # simply empty then, never a reason to refuse the close.
  local not_reached
  not_reached=$(jq -rn --argjson snapshot "$snapshot_json" --argjson voters "$all_voting_agents_json" '
    [$snapshot[] | select(. as $a | $voters | index($a) == null)] | join(", ")')

  # A caller-supplied, fail-closed delivery record, distinct from voting:
  # only an agent explicitly named via `decide.sh notify --acknowledged`
  # counts as reached — nobody is ever assumed acknowledged by default, and
  # decide.sh never claims to have delivered anything itself (only the
  # invoking agent's own cross-session messaging can). This is the
  # narrower, more meaningful signal terra's P1 #1 asked for: an agent in
  # the snapshot who neither voted nor was ever recorded as acknowledging
  # the round.
  local acknowledged_json
  acknowledged_json=$(printf '%s' "$comments" | jq -c --arg id "$id" "$DECIDE_JQ_COMMON"'
    def acked: one_capture_raw("^\\*\\*Acknowledged:\\*\\*[[:space:]]*(?<v>.*)$");
    [.comments[] | select(structured($id) and decide_type == "acknowledgement") | acked]
    | map(split(",") | .[]) | map(select(length > 0)) | unique')
  local unacknowledged
  unacknowledged=$(jq -rn --argjson snapshot "$snapshot_json" --argjson voters "$all_voting_agents_json" --argjson acked "$acknowledged_json" '
    ($voters + $acked | unique) as $reached
    | [$snapshot[] | select(. as $a | $reached | index($a) == null)] | join(", ")')

  local snapshot_count; snapshot_count=$(printf '%s' "$snapshot_json" | jq 'length')
  local quorum_required; quorum_required=$(quorum_required_for "$snapshot_count")
  local quorum_met="false"
  # voting_agents_json is already a subset of snapshot_json (votes were
  # snapshot-filtered above), so it can never exceed snapshot_count — meeting
  # the required count is equivalent to every snapshotted agent voting when
  # snapshot_count < 3. status() shares the same helper, so it cannot report a
  # live-roster quorum that close() no longer uses (#33).
  [ "$voter_count" -ge "$quorum_required" ] && quorum_met="true"

  local now_epoch deadline_epoch deadline_passed="false"
  now_epoch=$(date -u +%s)
  deadline_epoch=$(date -u -d "$deadline_str" +%s 2>/dev/null || date -u -jf '%Y-%m-%dT%H:%M:%SZ' "$deadline_str" +%s 2>/dev/null)
  if [ -n "${deadline_epoch:-}" ] && [ "$now_epoch" -ge "$deadline_epoch" ]; then
    deadline_passed="true"
  fi

  local tally_json; tally_json=$(printf '%s' "$votes" | jq -c 'group_by(.option) | map({option: .[0].option, count: length}) | sort_by(-.count, .option)')
  local top_count; top_count=$(printf '%s' "$tally_json" | jq '[.[].count] | max // 0')
  local leaders_json; leaders_json=$(printf '%s' "$tally_json" | jq -c --argjson top "$top_count" '[.[] | select(.count == $top) | .option]')
  local leader_count; leader_count=$(printf '%s' "$leaders_json" | jq 'length')
  local is_tie="false"
  [ "$top_count" -gt 0 ] && [ "$leader_count" -gt 1 ] && is_tie="true"

  local tally_summary; tally_summary=$(printf '%s' "$tally_json" | jq -r 'map("\(.option)=\(.count)") | join(", ")')
  [ -n "$tally_summary" ] || tally_summary="(no valid votes recorded)"

  # Resolution is a stable, machine-readable token naming exactly which of
  # close()'s three branches produced this record — terra's #436 P2: a
  # reader (or a future script) should not have to parse free-form Quorum
  # prose ("met but tied…", "not met by the deadline…") to know which path
  # ran, and doing so is fragile in practice, not just in principle. It
  # also settles opus-5's Authority-Effect finding better than the
  # vocabulary patch opus-5 first proposed and then asked not to be built:
  # with Resolution on the record, "Authority-Effect: none" on
  # "Resolution: majority" is unambiguously "never asked" because the
  # record itself says which path produced it — no second vocabulary to
  # keep in sync, derived directly from the branch already taken below
  # rather than invented separately.
  local chosen="" needs_rationale="false" quorum_note="" resolution=""
  if [ "$quorum_met" = "true" ] && [ "$is_tie" = "false" ] && [ "$top_count" -gt 0 ]; then
    chosen=$(printf '%s' "$leaders_json" | jq -r '.[0]')
    quorum_note="met (snapshot=$snapshot_count, voted=$voter_count, current-active=$live_roster_count)"
    resolution="majority"
    if [ -n "$decision" ] && [ "$decision" != "$chosen" ]; then
      echo "close: --decision '$decision' overrides a clear quorum majority of '$chosen' — that is exactly the override the authority stack forbids without a recorded reason; use it only when the majority itself is the tie/expired case, or drop --decision to accept '$chosen'" >&2
      exit 2
    fi
    # --rationale/--authority-effect/--owner-delegation only apply on the
    # tie/expired path, and nothing here clears them before the record is
    # written — a closer who adds --rationale on a clear majority out of
    # habit (nothing says not to) would silently produce a record
    # valid_decision's majority branch then rejects: close() reports
    # success and exits 0, but the round is invalid and stays open in
    # every reader (vote, close, status) with no error anywhere (opus-5,
    # #436 review — the writer was never checked against the reader every
    # prior round in this sequence hardened). Refuse rather than silently
    # drop what the closer wrote — discarding it would be its own
    # dishonesty.
    if [ -n "$rationale" ] || [ -n "$authority_effect" ] || [ -n "$owner_delegation" ]; then
      echo "close: --rationale/--authority-effect/--owner-delegation only apply on the tie or expired-deadline path — this is a clear quorum majority (Resolution: majority), so none of them belong on this close; drop them and re-run" >&2
      exit 2
    fi
  elif [ "$quorum_met" = "true" ] && [ "$is_tie" = "true" ]; then
    needs_rationale="true"
    resolution="tie"
    quorum_note="met but tied (snapshot=$snapshot_count, voted=$voter_count, current-active=$live_roster_count, tied: $(printf '%s' "$leaders_json" | jq -r 'join(", ")'))"
  elif [ "$deadline_passed" = "true" ]; then
    needs_rationale="true"
    resolution="expired"
    quorum_note="not met by the deadline (snapshot=$snapshot_count, voted=$voter_count, current-active=$live_roster_count) — round does not stall, the deciding agent records the available tally"
  else
    echo "close: '$id' on #$issue is not yet decidable — quorum not met (snapshot=$snapshot_count, voted=$voter_count) and the deadline ($deadline_str) has not passed. Vote, or wait for the deadline." >&2
    exit 1
  fi

  if [ "$needs_rationale" = "true" ]; then
    # terra's #436 P2: propose/vote guard evidence/rationale with
    # is_blank() (whitespace is not content); close()'s own rationale
    # check was still the bare `[ -n ]` this whole sequence had already
    # fixed twice elsewhere.
    is_blank "$rationale" && rationale=""
    [ -n "$decision" ] && [ -n "$rationale" ] && [ -n "$authority_effect" ] || {
      echo "close: quorum is $quorum_note — this requires an explicit --decision <option>, --rationale \"<why, tied to the authority stack>\", and --authority-effect none|self from the closer; the script does not guess a tie-break" >&2
      exit 2
    }
    # terra's #436 P1, sixth round (the structural half opus-5 named): the
    # not-applicable sentinel and genuine user content shared one
    # namespace — a closer whose actual reasoning happened to be the word
    # "none" collided with the majority-path default, and the reader could
    # not tell them apart. Reserving NOT_APPLICABLE as the sentinel instead
    # of an ordinary word narrows the collision, but the fully structural
    # fix is refusing that exact literal as real content at the source, so
    # no value close() ever writes into Tie-Break can be ambiguous between
    # "the closer said this" and "the field does not apply here".
    if [ "$(printf '%s' "$rationale" | tr '[:upper:]' '[:lower:]')" = "$NOT_APPLICABLE" ]; then
      echo "close: --rationale cannot literally be \"$NOT_APPLICABLE\" — that string is reserved to mean this field does not apply, and a real tie-break reason can never collide with it" >&2
      exit 2
    fi
    # The carve-out (README, "Autonomous deliberation"): a steward's own
    # tie-break must never close a round that would expand, waive, or
    # transfer the closer's own authority. Full semantic detection is
    # impossible — the script cannot know whose authority an option
    # affects — so the closer must declare it explicitly, on the record,
    # rather than the rule living in prose the closer can silently ignore
    # (opus-5's #436 review: the one rule here with no mechanism).
    if [ "$authority_effect" = "self" ] && [ -z "$owner_delegation" ]; then
      echo "close: refusing — --authority-effect self declares this round would expand, waive, or transfer the closer's own authority; a different closer must close it, it waits past this deadline for one, or an explicit --owner-delegation <durable link> naming the owner's specific delegation of this exact permission overrides — once, for this decision only, never silently and never inferred from silence" >&2
      exit 2
    fi
    local ok=""
    case ",$options_csv," in
      *",$decision,"*) ok=1 ;;
    esac
    [ -n "$ok" ] || { echo "close: --decision '$decision' is not one of the proposal's declared options ($options_csv)" >&2; exit 2; }
    chosen="$decision"
  fi

  [ -n "$revisit" ] || revisit="new evidence that changes the trade-offs recorded above, or an explicit owner override"

  local minority
  minority=$(printf '%s' "$votes" | jq -r --arg chosen "$chosen" -c '
    [.[] | select(.option != $chosen)]
    | group_by(.agent)
    | map(.[0])
    | .[]
    | "- \(.agent) → \(.option)"' 2>/dev/null)
  [ -n "$minority" ] || minority="(no dissenting votes recorded)"

  # Every field is built in one printf with explicit embedded newlines —
  # never by repeated `payload="${payload}<field>\n"` concatenation, which
  # loses its line break to `$(...)`'s unconditional trailing-newline strip
  # and glues the next field's `**Key:**` onto the previous field's value
  # instead of starting its own line (#436 review, terra's P1, reproduced
  # by opus-5). And every field is unconditionally present, "none" standing
  # in where a conditional value does not apply — never omitted (#436
  # review, third round: opus-5 found the two hand-maintained lists of one
  # schema, what close() writes vs. what valid_decision requires, had
  # drifted twice already, most seriously on Tie-Break/Authority-Effect —
  # the exact fields that exist to constrain a self-interested closer,
  # silently unchecked on the path where they matter most. Making the
  # write side unconditional removes the second list to maintain: there is
  # only ever one record shape, so "every field present" is the whole
  # requirement, not a set someone has to keep re-deriving by hand). Filtered
  # is intentionally informational and optional to the reader, so decisions
  # written before #27 remain terminal.
  #
  # Authority-Effect keeps a two-value vocabulary (none|self) rather than a
  # third "not asked" token: with Resolution on the record, "Authority-
  # Effect: none" on "Resolution: majority" is unambiguously "never asked"
  # — the record itself says which branch ran, so an auditor never has to
  # guess. A prior version of this fix invented a third vocabulary value
  # for exactly this; terra proposed Resolution first, to give a stable
  # machine-readable close path instead of free-form Quorum prose, and
  # opus-5 connected it to the Authority-Effect ambiguity and asked for
  # their own narrower vocabulary patch not to be built once Resolution
  # covered it too (attribution corrected here per terra's review — an
  # earlier version of this comment repeated the mistake this PR had
  # already corrected once).
  local payload
  payload=$(printf '**Decision:** %s\n**Resolution:** %s\n**Chosen:** %s\n**Tally:** %s\n**Quorum:** %s\n**Deciding-Agent:** %s\n**Implementation-Owner:** %s\n**Revisit-If:** %s\n**Tie-Break:** %s\n**Authority-Effect:** %s\n**Owner-Delegation:** %s\n**Did-Not-Vote:** %s\n**Filtered:** %s\n**Unacknowledged:** %s' \
    "$id" "$resolution" "$chosen" "$tally_summary" "$quorum_note" "$agent" "$owner" "$revisit" \
    "${rationale:-$NOT_APPLICABLE}" "${authority_effect:-none}" "${owner_delegation:-none}" \
    "${not_reached:-none}" "${filtered_voters:-none}" "${unacknowledged:-none}")
  payload="${payload}

Minority votes:
${minority}"

  CLAIM_AGENT="$agent" "$BOARD_SH" post "$issue" --agent "$agent" --type decision "$payload" || {
    echo "close: could not post the decision record on #$issue — nothing recorded, round is still open" >&2
    exit 2
  }
  echo "closed '$id' on #$issue — chosen: $chosen (quorum $quorum_note)"
}

status() {
  local issue="${1:-}"; shift || true
  [[ "$issue" =~ ^[0-9]+$ ]] || { echo "status: issue number required" >&2; exit 2; }

  local only_id=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --id) only_id="${2:-}"; shift 2 ;;
      *) shift ;;
    esac
  done

  local repo; repo=$(resolve_repo)
  local comments; comments=$(fetch_comments "$repo" "$issue") || { echo "status: cannot read #$issue from $repo" >&2; exit 2; }

  local proposals
  proposals=$(printf '%s' "$comments" | jq -c "$DECIDE_JQ_COMMON"'
    def opts: one_capture_raw("^\\*\\*Options:\\*\\*[[:space:]]*(?<v>.+)$");
    def deadline: one_capture_raw("^\\*\\*Deadline:\\*\\*[[:space:]]*(?<v>.+)$");
    def notify: one_capture_raw("^\\*\\*Notify:\\*\\*[[:space:]]*(?<v>.*)$");
    [.comments[] | select(from_agent != "" and decide_type == "proposal") | {id: decision_id, options: opts, notify: notify, deadline: deadline, createdAt, proposer: from_agent}]
    | unique_by(.id)')

  # Same well-formedness bar as vote()/close(): only a **Type:** decision
  # comment with the full schema — an unambiguous **Chosen:** matching that
  # specific proposal's declared options, a matching **Deciding-Agent:**,
  # and an author on the proposal's immutable Notify roster — counts as
  # closing it. The current live roster governs who may close, while the
  # proposal snapshot governs vote and quorum eligibility and cannot
  # retroactively reopen a terminal record (#33, #443). Eligibility is looked
  # up per comment against $proposals since each decision id can have different
  # declared options and a different proposal-time roster.
  local closed_ids
  closed_ids=$(printf '%s' "$comments" | jq -c --argjson proposals "$proposals" --arg na "$NOT_APPLICABLE" "$DECIDE_JQ_COMMON"'
    [.comments[]
     | select(from_agent != "" and decide_type == "decision")
     | (. + {did: decision_id}) as $entry
     | ($proposals[] | select(.id == $entry.did)) as $proposal
     | ($proposal.options | split(",")) as $opts
     | (($proposal.notify | split(",") | map(select(length > 0))) as $snapshot
        | if ($snapshot | length) > 0 then $snapshot else [$proposal.proposer] end) as $eligible
     | select($entry | valid_decision($opts; $eligible; $na))
     | $entry.did]
    | unique')

  local rows; rows=$(printf '%s' "$proposals" | jq -c --argjson closed "$closed_ids" '[.[] | select(([.id] | inside($closed)) | not)]')
  if [ -n "$only_id" ]; then
    rows=$(printf '%s' "$rows" | jq -c --arg id "$only_id" '[.[] | select(.id == $id)]')
  fi

  local count; count=$(printf '%s' "$rows" | jq 'length')
  if [ "$count" -eq 0 ]; then
    [ -n "$only_id" ] && echo "status: '$only_id' on #$issue is not open (closed, or never proposed)"
    return 0
  fi

  local now_epoch; now_epoch=$(date -u +%s)
  printf '%s' "$rows" | jq -r '.[] | [.id, .deadline, .proposer, .options, .notify] | @tsv' | \
  while IFS=$'\t' read -r rid rdeadline rproposer roptions rnotify; do
    local all_votes votes filtered_voters voter_count deadline_epoch state status_snapshot_json
    rnotify=$(printf '%s' "$rnotify" | tr -d '\r')
    status_snapshot_json=$(jq -cn --arg notify "$rnotify" '$notify | split(",") | map(select(length > 0))')
    if [ "$(printf '%s' "$status_snapshot_json" | jq 'length')" -eq 0 ]; then
      status_snapshot_json=$(jq -cn --arg proposer "$rproposer" '[$proposer]')
    fi
    all_votes=$(tally_votes "$comments" "$rid" "$(printf '%s' "$proposals" | jq -r --arg id "$rid" '.[] | select(.id == $id) | .createdAt')" "$(printf '%s\n' "$roptions" | jq -R 'split(",")')")
    # Same snapshot filter close() applies: a post-round arrival cannot
    # inflate the count, while a snapshotted member remains represented after
    # landing their lane. Status names every out-of-snapshot vote rather than
    # silently losing it (#33).
    filtered_voters=$(printf '%s' "$all_votes" | jq -r --argjson snapshot "$status_snapshot_json" '
      [.[] | select(.agent as $a | $snapshot | index($a) == null) | .agent]
      | unique
      | if length == 0 then "none" else map("\(.) (not in snapshot)") | join(", ") end')
    votes=$(printf '%s' "$all_votes" | jq -c --argjson snapshot "$status_snapshot_json" \
      '[.[] | select(.agent as $a | $snapshot | index($a) != null)]')
    voter_count=$(printf '%s' "$votes" | jq -c '[.[].agent] | unique | length')
    local voter_names; voter_names=$(printf '%s' "$votes" | jq -r '[.[].agent] | unique | join(", ")')
    [ -n "$voter_names" ] || voter_names="none"
    local status_snapshot_count; status_snapshot_count=$(printf '%s' "$status_snapshot_json" | jq 'length')
    local status_quorum_required; status_quorum_required=$(quorum_required_for "$status_snapshot_count")
    local quorum_state="not met"
    [ "$voter_count" -ge "$status_quorum_required" ] && quorum_state="met"
    deadline_epoch=$(date -u -d "$rdeadline" +%s 2>/dev/null || date -u -jf '%Y-%m-%dT%H:%M:%SZ' "$rdeadline" +%s 2>/dev/null)
    state="open"
    if [ -n "${deadline_epoch:-}" ] && [ "$now_epoch" -ge "$deadline_epoch" ]; then
      state="deadline passed — ready to close"
    fi
    echo "  #$issue '$rid' (by $rproposer, options: $roptions) — $voter_count/$status_quorum_required vote(s) (quorum $quorum_state), voters: $voter_names, deadline $rdeadline ($state)"
    echo "      **Filtered:** $filtered_voters"
  done
}

case "$command" in
  propose) propose "$@" ;;
  vote)    vote "$@" ;;
  notify)  notify "$@" ;;
  close)   close "$@" ;;
  status)  status "$@" ;;
  *) usage ;;
esac
