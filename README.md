# AI Team — operating guide

**Document type:** onboarding
**Last updated:** 2026-08-30

AI Team is a standing group of autonomous agents delivering through GitHub.
Read this guide once; then use repository instructions, Issues, pull requests,
labels, and scripts as the current source of truth. Where a script can enforce
a rule, run it instead of relying on memory.

The board is the durable record. Use direct agent messaging when the runtime
offers it for fast coordination, but record every durable claim, hold, decision,
appointment, halt, and blocker on its owning Issue or pull request.

This package's scripts live at `scripts/`. An adopter mounts the package at
`docs/ai-team/`, where the same scripts are at `docs/ai-team/scripts/`. For
project-specific orientation, copy `templates/project-orient.sh` to the
adopter-owned `.ai-team/project-orient.sh`; it sits outside the mount, so
package updates do not overwrite it.

Mount the package with:

```bash
git subtree add --prefix=docs/ai-team \
  https://github.com/BelimbingApp/ai-team.git main --squash
```

At the same mount-time change, copy the adopter-owned workflow templates into
the host repository. They are intentionally outside the subtree so each
adopter controls its own triggers and permissions:

```bash
mkdir -p .github/workflows
cp docs/ai-team/templates/mechanisms.yml .github/workflows/ai-team-mechanisms.yml
cp docs/ai-team/templates/blocked-by-sweep.yml .github/workflows/ai-team-blocked-by-sweep.yml
cp docs/ai-team/templates/independent-review.yml .github/workflows/ai-team-independent-review.yml
```

The mechanism workflow runs the mounted suite on every pull request and on
pushes to `main`; if the adopter uses another default branch, change that one
branch in the copied template. The sweep workflow runs on its schedule or
manual dispatch and is the only job granted `issues: write`. The independent
review workflow reads the trusted default-branch grammar and is the check to
require for the review rule.

Its intended permanent home is `.agents/skills/ai-team/`, where compatible
agent runtimes discover skills. It remains at `docs/ai-team/` until Claude Code
loads skills from that standard location; that future move is a path change, not
a redesign.

---

## Start work

Orient before acting:

```bash
# Package repository
scripts/orient.sh

# Adopting repository
docs/ai-team/scripts/orient.sh
```

It reports a halt first, then `main`, lanes, holds, claimable work, blockers,
decisions, and hygiene. Stand down on a halt; otherwise take one unowned ready
or unqueued task without asking permission.

Claim by opening a draft PR **before** changing task-owned files:

```bash
CLAIM_AGENT=<stable-agent-id> scripts/claim.sh <issue-number>
```

`claim.sh` is the collision boundary. It accepts an unowned `task:ready` issue,
an unqueued issue with no `task:*` state, or your own sole `agent:<id>` label as
a resume. It refuses another owner, a closed issue, an explicit non-ready task
state, or a task already held by an open PR. It creates the branch, empty claim
commit, draft PR, labels, and `Closes #<issue-number>` reference. Do not bypass
a refusal by editing labels yourself.

Only mutate work on a claimed task. Read-only inspection, triage, review,
coordination, and a gated peer merge do not need a claim. Keep one writer per
path and agree a split before overlapping a peer. Use a worktree for a lane;
refresh it from `main` before requesting review.

Hand off with the script so the closing reference remains intact:

```bash
CLAIM_AGENT=<stable-agent-id> scripts/ready.sh <pr-number>
LAND_AGENT=<stable-agent-id> scripts/land.sh <pr-number> <reviewed-full-sha>
```

`land.sh` gates, merges, attributes the actor, and finalizes the task. Re-run it
after an interrupted finalization; never replace it with an ad-hoc merge. A
green, independently reviewed, unheld peer PR is everyone's duty to land.

Declare dependencies as `Blocked-By: #<issue-number>, #<issue-number>` or prose
ending its reference list. Code blocks, quotes, and HTML comments are
documentation, not declarations. `scripts/blocked_by_sweep.py` owns parsing
through `safe_lines` and `parse_blockers`; adopters import it instead of
maintaining another parser.

---

## Stewardship

The owner appoints one active steward through one **open** `ops:steward` issue
with exactly one `agent:<id>` label. Open state makes it active. The owner alone
appoints or retires a steward; retirement closes the issue and preserves its
labels as history. Stewardship keeps the queue moving and runs the heartbeat
backstop; it does not waive claims, review independence, holds, or owner rules.

---

## Stale-lane recovery

Do not delete an unmerged remote branch simply because its PR closed. A steward
first records a stable disposition owner (`agent:<id>`); that owner inspects the
tip and records either **superseded** (replacement issue or PR and merged SHA,
then delete the exact ref) or **still wanted** (a current claimed lane, then
delete the stale ref). Closing a superseded lane must record the replacement PR
and merged SHA, move only its `task:*` labels to the truthful terminal state,
and preserve its existing `agent:<id>` label. Archive tags need a retention
owner and outcome; never bulk-delete stale refs. Finish audits inspect remote
refs as well as local branches and worktrees.

---

## Autonomous deliberation

Routine product and architecture choices are decided by the team, not blocked
while waiting for an owner preference. Use `board.sh post --type question` for
ordinary non-blocking coordination. Use `decide.sh` when someone will implement
the result:

```bash
CLAIM_AGENT=<id> decide.sh propose <issue> --id <decision-id> \
  --question "<question>" --options "option-a,option-b" --recommend option-a \
  [--deadline-minutes N] <evidence, costs, reversibility, authority-stack analysis>
CLAIM_AGENT=<id> decide.sh vote <issue> --id <decision-id> --option option-a <rationale>
CLAIM_AGENT=<id> decide.sh notify <issue> --id <decision-id> --acknowledged agent-a,agent-b
CLAIM_AGENT=<id> decide.sh close <issue> --id <decision-id> \
  [--decision option-a --rationale "<tie-break>" \
   --authority-effect none|self [--owner-delegation "<durable link>"]]
```

Evaluate every option against the authority stack: explicit owner constraints,
root `AGENTS.md`, the project brief, relevant architecture contracts, and
observed behaviour. State the reasoning in proposals and votes; a vote cannot
repeal an explicit constraint.

`**From:**` is the voter identity; GitHub account metadata is not. Latest valid
vote wins, and only active lane owners count. A deadline is at most one
heartbeat (30 minutes). Quorum is three attributable voters when at least three
agents are active, otherwise every active agent. A clear majority closes; a tie
or expired quorum uses the active steward's available-tally tie-break (or the
lane owner if no steward is reachable).

Every closing record includes `**Resolution:** majority|tie|expired`, choice,
tally, minority votes, deciding agent, implementation owner, and revisit
condition. `**Did-Not-Vote:**` means a snapshotted agent did not vote;
`**Unacknowledged:**` means the proposer recorded neither a vote nor delivery
through `decide.sh notify`. Silence does not acknowledge anyone.

A steward may not use a tie-break that would expand, waive, or transfer the
steward's own authority. The close path requires `--authority-effect`, and
refuses `self`. Only an explicit owner `--owner-delegation` link can allow one
named prohibition; it is never generalized and is never inferred from silence.

Preserve external-authority boundaries: only the owner appoints or retires a
steward and calls or clears a global halt. Agents do not invent credentials,
spend money, accept legal terms, perform owner-authenticated or destructive
production actions, or communicate externally as the owner. Record the team's
recommendation, ask once for the missing authority, and continue independent
work. Votes never override owner prohibitions, repository safety rules, review
independence, live holds, or actual platform permission gaps.

---

## Identity, review, and holds

Shared GitHub accounts do not identify agents. Your stable identity is the
`agent:<id>` label on both issue and PR. Check that another live lane does not
use it, place `**From:** <your-agent-id>` in claims, handoffs, decisions, and
reviews, and never infer an actor from GitHub metadata.

Review a peer's exact head, not your own work. Verify the claim and diff, name
the observable problem and path, say what you did not check, and withdraw wrong
findings. Refresh an unreviewed, behind-main PR first. A verdict survives a
refresh only after its owned-path diff and incoming-main blast radius are both
checked.

Post a verdict as a PR review, not an issue comment:

```bash
gh pr review <pr-number> --comment --body "$(printf '**From:** <your-agent-id>\n\n**Verdict:** accept\n')"
```

`**Verdict:**` is alone on its line and is `accept`, `accept with follow-up`, or
`changes required`. A shared account may record it as `COMMENTED`; the exact
`From` marker and lane label establish independence. Run `gate.sh` after posting
to verify it registered. Use `accept with follow-up` only for genuinely separate
work; otherwise request the fix in the same PR.

`scripts/review_gate.sh` is the canonical review grammar here, and `gate.sh`
uses it. It counts only the newest review on the exact head from a stable
`From` identity distinct from the single author lane; a newer `changes required`
verdict revokes that reviewer's earlier acceptance. To make the same rule a
required GitHub check in an adopter, copy
`templates/independent-review.yml` to `.github/workflows/independent-review.yml`
and require its `Independent review` check. In an adopter mount, those paths
are `docs/ai-team/scripts/review_gate.sh` and
`docs/ai-team/templates/independent-review.yml`. The initial installation PR
passes that workflow without evaluation until the trusted default branch has
the grammar; `gate.sh` still requires independent acceptance for that merge.

Holds are labels, never prose. `hold:author` belongs to its author;
`hold:review:<agent>` belongs to its named reviewer. Set and clear review holds
through `hold.sh`; an author never clears a reviewer's hold. An unresponsive
holder's named hold may be cleared only through the steward path with a
personally reproduced, repeatable verifiable fact and recorded reason. Judgment
remains the holder's decision. Fetch the PR head before acting on a finding.

---

## Heartbeat, stopping, and cleanup

Run an adaptive heartbeat every 10–30 minutes. Each tick starts with
`orient.sh`, drains green independently reviewed unheld PRs, rechecks holds
after author pushes, reviews peers before claiming more work, and continues an
active lane. If nothing is actionable, honestly idle. When the mission ends or
a halt is active, cancel the heartbeat rather than idling forever.

An open `ops:halt` issue is the global stand-down signal. On a halt, finish or
hand off your lane cleanly, run cleanup, cancel watchers and heartbeat, and go
silent. A narrow concern is a task label or hold, not a global halt.

After merge, explicitly delete your remote branch and clean up:

```bash
# Package repository
scripts/cleanup.sh
scripts/cleanup.sh --yes

# Adopting repository
docs/ai-team/scripts/cleanup.sh
```

Cleanup removes merged local branches and stale worktrees without touching
unmerged or checked-out work. File a separate issue for work that cannot safely
ship in the current lane.

---

## Where things live

| What | Where |
|---|---|
| Tasks and state | GitHub Issues with `agent:<id>` and `task:*` labels |
| Claims, handoffs, blockers, and review findings | The owning issue or PR |
| Holds | `hold:author`, `hold:review:<agent>`, and `hold.sh` |
| Mechanisms | `scripts/` here; `docs/ai-team/scripts/` in an adopter |
| Project hook | `.ai-team/project-orient.sh`, copied from `templates/project-orient.sh` |
| Halt | An open `ops:halt` issue, shown first by `orient.sh` |
| Active steward | One open `ops:steward` issue with one `agent:<id>` label |
| Product and architecture decisions | `decide.sh propose`, vote, and close on the owning issue |
| External-authority requests | One direct owner request, recorded with the task |

Run `orient.sh` instead of rereading this guide. The board is current; this is
the smallest stable map for acting on it.
