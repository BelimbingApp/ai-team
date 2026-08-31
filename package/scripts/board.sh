#!/usr/bin/env bash
# board.sh — structured posting and digest reading for the shared board (#363).
#
# The board is the one channel guaranteed to span every harness and machine
# (#360), but raw threads burn tokens at READ time and prose posting policy is
# forgotten at the binding moment. This applies the mechanism pattern that
# fixed holds (labels, not prose) and verdicts (parsed markers, #359) to
# posting and reading generally:
#
#   posts without the machine header are invisible to team tooling.
#
#   board.sh post <n> --agent <id> --type <status|finding|question|handoff|proposal|vote|decision|acknowledgement> [body…]
#       Stamp the header gate.sh parses, enforce a visible-byte budget
#       (BOARD_POST_BUDGET, default 1400), fold overflow into <details>.
#       Refuses --type verdict: verdicts must be PR reviews or the gate
#       cannot see them (#359). proposal/vote/decision are decide.sh's
#       (#430) — this script only stamps and budgets them, decide.sh owns
#       their field grammar and tally semantics.
#   board.sh digest <n>
#       Title/state/labels, then structured posts only — <details> stripped,
#       long posts truncated to BOARD_DIGEST_LINES (default 12) — and one
#       summary line counting unstructured posts instead of rendering them.
#   board.sh hygiene
#       Per-thread unstructured-post counts over active lanes only (open PRs
#       and open agent:* issues), for orient.sh to surface.

# pipefail: a failed gh read must fail the digest loudly, not hand jq empty
# input and exit 0 (sol's finding on #364).
set -uo pipefail

# No repository default. A vendored copy that names the origin project posts an
# adopter's status onto the wrong board, and board.sh never consults gh, so being
# in the right working directory does not save you (#445). Resolve from the
# origin remote, which is the repo this checkout actually belongs to.
BOARD_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
# shellcheck source=docs/ai-team/scripts/_default_branch.sh
# shellcheck disable=SC1091
source "$BOARD_DIR/_default_branch.sh"
REPO="${BOARD_REPO:-$(ai_team_origin_repo 2>/dev/null || true)}"
# Fall back to gh's ambient answer rather than failing: a checkout whose origin
# is not a GitHub URL still has a repository gh can name. Order matters — origin
# is asked first because it is the repo this checkout belongs to, and gh can be
# pointed elsewhere by remote.<name>.gh-resolved.
[ -n "$REPO" ] || REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)
if [ -z "$REPO" ]; then
  echo "board.sh: cannot determine the repository. Set BOARD_REPO=<owner>/<repo>," >&2
  echo "or run inside a checkout whose origin remote points at a GitHub repository." >&2
  exit 2
fi
BUDGET="${BOARD_POST_BUDGET:-1400}"
DIGEST_LINES="${BOARD_DIGEST_LINES:-12}"
# Accounts whose posts no agent authored and no digest should render or nag
# about: CI bots. Human accounts are never listed here — an unheadered post
# from a human account may be the OWNER, whose rulings outrank every marker
# (#364 P1), so those render; hygiene still counts them because the same
# shared account may be a forgetful agent, and both readings want the flag.
BOTS="${BOARD_BOTS:-sonarqubecloud dependabot github-actions}"

usage() {
  sed -n '2,3p;12,23p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

command="${1:-}"
[ -n "$command" ] || usage
shift

post() {
  local number="" agent="${CLAIM_AGENT:-${BOARD_AGENT:-}}" type="" body="" body_file=""

  number="${1:-}"
  [ -n "$number" ] || { echo "post: issue/PR number required" >&2; exit 2; }
  shift

  while [ $# -gt 0 ]; do
    case "$1" in
      --agent) agent="${2:-}"; shift 2 ;;
      --type) type="${2:-}"; shift 2 ;;
      --body-file) body_file="${2:-}"; shift 2 ;;
      *) body="${body:+$body }$1"; shift ;;
    esac
  done

  if [ -z "$agent" ]; then
    echo "post: agent id required (--agent, CLAIM_AGENT, or BOARD_AGENT)" >&2
    exit 2
  fi

  case "$type" in
    status|finding|question|handoff|proposal|vote|decision|acknowledgement) ;;
    verdict*)
      echo "post: refusing — a verdict posted as an issue comment is invisible to gate.sh (#359)." >&2
      echo "      Record it as a PR review instead:" >&2
      echo "      gh pr review $number --comment --body '**From:** $agent" >&2
      echo "" >&2
      echo "      **Verdict:** accept'" >&2
      exit 3
      ;;
    *)
      echo "post: --type must be one of status|finding|question|handoff|proposal|vote|decision|acknowledgement (got '${type:-none}')" >&2
      exit 2
      ;;
  esac

  if [ -n "$body_file" ]; then
    body=$(cat "$body_file") || exit 2
  elif [ -z "$body" ] && [ ! -t 0 ]; then
    body=$(cat)
  fi
  [ -n "$body" ] || { echo "post: empty body" >&2; exit 2; }

  # Split at the last line boundary inside the budget: the visible part stays
  # scannable, the remainder survives for humans inside a fold that digest
  # readers never pay for.
  # Byte-safe split (#364 P3): head -c can cut inside a multibyte character
  # when the budget window holds no newline, and bash ${#var} counts characters
  # under a UTF-8 locale while head -c counts bytes — so all arithmetic here is
  # in bytes (wc -c / tail -c), and a partial trailing sequence is dropped from
  # the visible part (iconv -c) into the fold, conserving every input byte.
  local visible="$body" folded="" total_bytes visible_bytes trimmed
  total_bytes=$(printf '%s' "$body" | wc -c)
  if [ "$total_bytes" -gt "$BUDGET" ]; then
    visible=$(printf '%s' "$body" | head -c "$BUDGET")
    case "$visible" in
      *$'\n'*) visible="${visible%$'\n'*}" ;;
      *)
        # glibc iconv exits 1 on an incomplete trailing sequence even though
        # -c has already written the correct truncated output (measured), and
        # EMPTY output is the correct answer for a budget smaller than one
        # character — so the output is taken unconditionally: reading exit
        # status or emptiness as a claim about correctness was the same error
        # at two successive layers (#364, sol's P2).
        #
        # Without iconv the trade inverts (#369): dropping the trim would have
        # made EVERY over-budget single-line post publish an empty visible
        # section, and one possibly-split trailing character is the lesser
        # harm than an empty post. Checked explicitly, not inferred from
        # iconv's output, which is legitimately empty at tiny budgets.
        if command -v iconv >/dev/null 2>&1; then
          visible=$(printf '%s' "$visible" | iconv -f UTF-8 -t UTF-8 -c 2>/dev/null || true)
        fi
        ;;
    esac
    visible_bytes=$(printf '%s' "$visible" | wc -c)
    folded=$(printf '%s' "$body" | tail -c +"$((visible_bytes + 1))")
    folded="${folded#$'\n'}"
  fi

  {
    printf '**From:** %s\n\n**Type:** %s\n\n%s\n' "$agent" "$type" "$visible"
    if [ -n "$folded" ]; then
      printf '\n<details>\n<summary>full detail (folded by board.sh — over the %s-byte visible budget)</summary>\n\n%s\n\n</details>\n' "$BUDGET" "$folded"
    fi
  } | gh issue comment "$number" --repo "$REPO" --body-file -
}

digest() {
  local number="${1:-}"
  [ -n "$number" ] || { echo "digest: issue/PR number required" >&2; exit 2; }

  # gh's built-in --jq lacks --arg and full regex support; gate.sh's idiom —
  # fetch JSON with gh, transform with the real jq binary — applies here too.
  #
  # Verdicts live in the REVIEW stream, not the conversation stream — the same
  # split behind #359, and post itself points verdict-writers at gh pr review —
  # so a digest reading only issue comments hides every verdict, including a
  # blocking one (sol's P1a on #364). Merge pulls/:n/reviews in; a plain issue
  # 404s there and contributes nothing.
  local issue_json reviews_json
  issue_json=$(gh issue view "$number" --repo "$REPO" --json number,title,state,labels,comments 2>/dev/null) \
    || { echo "digest: could not read #$number from $REPO" >&2; exit 1; }
  reviews_json=$(gh api "repos/$REPO/pulls/$number/reviews" --paginate 2>/dev/null | jq -s 'add // []' 2>/dev/null) || reviews_json='[]'
  [ -n "$reviews_json" ] || reviews_json='[]'

  printf '%s' "$issue_json" \
    | jq -r --argjson lines "$DIGEST_LINES" --arg bots "$BOTS" --argjson reviews "$reviews_json" '
      def is_bot: (.author.login // "") as $l | ($bots | split(" ")) | any(. == $l);
      # Match the gate.sh attribution contract exactly: scan every line, accept
      # one unambiguous stable identity, and reject conflicting identities.
      def from_agents:
        [((.body // "") | split("\n")[]
          | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<agent>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").agent
          | ascii_downcase)] | unique;
      def from_agent:
        from_agents as $agents
        | if ($agents | length) == 1 then $agents[0] else "" end;
      def structured: from_agent != "";
      def strip_and_trim(skip_header):
        (.body // "")
        | split("\n")
        | map(select((skip_header and test("^\\*\\*From:\\*\\*[[:space:]]*[a-z0-9]+(?:[._-][a-z0-9]+)*(?:[[:space:]]|$)"; "i")) | not))
        # Drop <details> blocks line-by-line rather than by multiline regex:
        # portable across jq builds, and the fold marker shows a reader that
        # evidence exists without charging them for it.
        | reduce .[] as $l ({inside: false, out: []};
            if ($l | test("^\\s*<details>")) then .inside = true | .out += ["[folded detail omitted]"]
            elif ($l | test("^\\s*</details>")) then .inside = false
            elif .inside then .
            else .out += [$l]
            end)
        | .out
        | map(select(. != "" and (test("^\\*\\*Type:\\*\\*") | not)))
        | ( if length > $lines
            then .[:$lines] + ["(+\(length - $lines) more lines — read the thread only if you need them)"]
            else .
            end )
        | map("   " + .)
        | join("\n");
      "== #\(.number) [\(.state)] \(.title)",
      "   labels: \([.labels[].name] | join(","))",
      ( ( .comments
          + ( $reviews
              | map(select((.body // "") != "")
                    | { body: .body,
                        createdAt: (.submitted_at // ""),
                        author: {login: (.user.login // "?")},
                        tag: ("[review \(.state // "")\(if .commit_id then " @" + .commit_id[0:7] else "" end)] ") }) ) )
        | sort_by(.createdAt) ) as $stream
      | ( ([$stream[] | select(is_bot)] | length) as $bot_count
        | [$stream[] | select(is_bot | not)] as $human
        | ( $human[]
            | (.tag // "") as $tag
            | if structured
              then "-- \($tag)\(from_agent) · \(.createdAt)",
                   strip_and_trim(true)
              # No header, human account: possibly the owner, whose posts
              # outrank every marker (#364 P1) — render, never hide.
              else "-- \($tag)[no header] \(.author.login // "?") · \(.createdAt)",
                   strip_and_trim(false)
              end ),
          ( if $bot_count > 0
            then "-- \($bot_count) bot post(s) ignored (\($bots))"
            else empty
            end ) )
    '
}

hygiene() {
  echo "== board hygiene — unstructured posts are invisible to digests =="

  local items
  items=$( {
    gh pr list --repo "$REPO" --state open --limit 20 --json number --jq '.[].number' 2>/dev/null
    gh issue list --repo "$REPO" --state open --limit 30 --json number,labels \
      --jq '.[] | select([.labels[].name] | any(startswith("agent:"))) | .number' 2>/dev/null
  } | sort -un )

  [ -n "$items" ] || { echo "  (no active lanes, or gh unavailable)"; return 0; }

  local n count clean=1
  for n in $items; do
    count=$(gh issue view "$n" --repo "$REPO" --json comments 2>/dev/null \
      | jq -r --arg bots "$BOTS" '
          def is_bot: (.author.login // "") as $l | ($bots | split(" ")) | any(. == $l);
          def from_agent:
            ([((.body // "") | split("\n")[]
               | capture("^\\*\\*From:\\*\\*[[:space:]]*(?<agent>[a-z0-9]+(?:[._-][a-z0-9]+)*)(?:[[:space:]]|$)"; "i").agent
               | ascii_downcase)] | unique) as $agents
            | if ($agents | length) == 1 then $agents[0] else "" end;
          [.comments[]
           | select(is_bot | not)
           | select(from_agent == "")]
          | length' 2>/dev/null) || continue
    if [ -n "$count" ] && [ "$count" -gt 0 ]; then
      echo "  #$n has $count unstructured post(s) — post via board.sh so tooling can see them"
      clean=0
    fi
  done
  if [ "$clean" -eq 1 ]; then
    echo "  ok      every post on active lanes carries the machine header"
  fi
  return 0
}

case "$command" in
  post) post "$@" ;;
  digest) digest "$@" ;;
  hygiene) hygiene "$@" ;;
  *) usage ;;
esac
