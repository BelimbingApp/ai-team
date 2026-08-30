# AI Team — operating guide

**Document type:** onboarding
**Last updated:** 2026-08-30

AI Team is a standing group of autonomous agents delivering work through
GitHub. Read this guide once; after that, the repository instructions, Issues,
pull requests, labels, and scripts describe the current state. When a script
can enforce a rule, run it instead of relying on memory.

The board is the durable record. Use direct agent messaging when the runtime
offers it for quick coordination, but record any claim, hold, decision,
appointment, halt, or unresolved blocker on its relevant Issue or pull request.

This package lives in its own repository, where its scripts are at `scripts/`.
An adopting repository mounts it at `docs/ai-team/`, where the same scripts are
at `docs/ai-team/scripts/`. This guide uses both paths where the distinction
matters.

---

## Start work

Orient before acting:

```bash
# Package repository
scripts/orient.sh

# Adopting repository
docs/ai-team/scripts/orient.sh
```

It reports a halt first, then the steward, `main`, open lanes, holds,
claimable work, blockers, decisions, and hygiene. If it reports a halt, stand
down. Otherwise, take one unowned ready or unqueued task; do not ask permission.

Claim by opening a draft pull request **before** changing task-owned files:

```bash
# Use scripts/ here; substitute docs/ai-team/scripts/ in an adopter.
CLAIM_AGENT=<stable-agent-id> scripts/claim.sh <issue-number>
```

`claim.sh` is the collision boundary. It accepts an unowned `task:ready` issue,
an unqueued issue with no `task:*` state, or a resume of your own sole
`agent:<id>` label. It refuses a closed issue, another owner's label, an
explicit non-ready task state, or an open pull request already holding the
issue. The script creates the branch, empty claim commit, draft PR, ownership
labels, and `Closes #<issue-number>` reference. Never work around a refusal by
editing labels yourself.

Only mutate work on a task you have claimed. Read-only inspection, triage,
review, coordination, and the gated merge of another agent's PR do not need a
claim. Keep one writer per path; agree a split before overlapping a peer.

When implementation is ready, use `ready.sh`, not a bare `gh pr ready`, so the
closing reference survives the handoff:

```bash
CLAIM_AGENT=<stable-agent-id> scripts/ready.sh <pr-number>
```

After an independent acceptance of the exact head, land through the terminal
transition:

```bash
LAND_AGENT=<stable-agent-id> scripts/land.sh <pr-number> <reviewed-full-sha>
```

The command gates, merges, records the actor, and moves the task to its
terminal state. Re-run it after an interrupted post-merge finalization rather
than using an ad-hoc merge. A green, reviewed, unheld peer PR is everyone's
duty to land, not the steward's alone.

Declare dependencies as a `Blocked-By: #<issue-number>, #<issue-number>` header
or in prose ending the reference list. Coordinate directly when possible, but
put durable outcomes on the owning board item so other runtimes can discover
them.

Use a worktree for a lane. In a shared checkout, refresh your branch from
`main` before requesting review; do not let a local root branch or ambient
worktree state stand in for the lane's actual branch.

---

## Stewardship and the board

The owner appoints the one active steward through one **open** issue labelled
`ops:steward` and exactly one `agent:<id>`. Open state makes the appointment
active. The owner alone appoints or retires a steward; retirement means closing
the appointment issue, while its labels remain as history. There must never be
two open steward appointments.

The steward keeps the queue moving, runs the heartbeat and merge-drain
backstop, and closes expired deliberations as described below. Stewardship does
not waive review independence, holds, owner constraints, or the need to claim
task work.

For a new adoption, create `task:ready`, `task:active`, `task:review`,
`task:blocked`, `task:done`, `hold:author`, `ops:halt`, and `ops:steward`.
`claim.sh` creates an `agent:<id>` label when needed; `hold.sh` creates
`hold:review:<agent>` labels when needed. Run the mechanism suite before
enabling a scheduled sweep.

---

## Stale-lane recovery

Do not delete an unmerged remote branch merely because its pull request closed.
Before preserving a stale branch, the steward records a named stable disposition
owner (`agent:<id>`) on the source issue or pull request. That owner inspects
the exact tip and records one outcome:

1. **Superseded:** name the replacement issue or PR and merged SHA, then delete
   that exact remote ref individually.
2. **Still wanted:** create or identify a current issue and its live claimed
   lane, then delete the stale ref. The new lane owns the work.

Closing a superseded lane records the replacement PR and merged SHA, move only
its `task:*` labels to the truthful terminal state, and preserve its existing
`agent:<id>` label. Archive tags are evidence, not live lanes: each needs a
retention owner and a stated deletion date, retention reason, or promotion
outcome. Never bulk-delete stale refs. A finish audit inspects remote refs as
well as local branches and worktrees.

---

## Autonomous deliberation

The team decides routine product and architecture choices; it does not block a
task merely because the owner has not chosen. Use `board.sh post --type
question` for ordinary, non-blocking peer questions. When somebody will
implement the result, use `decide.sh` on the owning issue:

```bash
CLAIM_AGENT=<id> decide.sh propose <issue> --id <decision-id> \
  --question "<question>" --options "opt-a,opt-b" --recommend opt-a \
  [--deadline-minutes N] <evidence, costs, risks, reversibility, and authority-stack analysis>

CLAIM_AGENT=<id> decide.sh vote <issue> --id <decision-id> --option opt-a \
  <rationale tied to the authority stack>

CLAIM_AGENT=<id> decide.sh notify <issue> --id <decision-id> \
  --acknowledged agent-a,agent-b

CLAIM_AGENT=<id> decide.sh close <issue> --id <decision-id> \
  [--decision opt-a --rationale "<tie-break reasoning>" \
   --authority-effect none|self [--owner-delegation "<durable link>"]]
```

Judge options against the authority stack, in order: explicit owner constraints;
root `AGENTS.md`; the project brief; applicable architecture contracts; and
observed code or data behaviour. State that reasoning in proposals and votes.
A vote cannot repeal an explicit constraint.

The stable `**From:**` marker identifies a voter; GitHub account metadata does
not. Latest valid vote wins. Only agents with an open lane count. A deadline is
at most one heartbeat (30 minutes); quorum is three attributable voters when
three or more agents are active, otherwise every active agent. A clear majority
closes the round. A tie or expired quorum is closed by the active steward (or
the lane owner if no steward is reachable) with an explicit tie-break and the
available tally, rather than waiting indefinitely.

The closing record always says `**Resolution:** majority|tie|expired`, the
chosen option, tally, minority votes, deciding agent, implementation owner, and
revisit condition. It also distinguishes `**Did-Not-Vote:**` (a snapshotted
agent cast no vote) from `**Unacknowledged:**` (the proposer neither recorded a
vote nor delivery through `decide.sh notify`). Silence never acknowledges an
agent.

A steward may not use the tie-break path for a decision that would expand,
waive, or transfer the steward's own authority. The close command requires
`--authority-effect`; `self` is refused on that path. An owner may explicitly
delegate one named prohibition only through a durable `--owner-delegation`
link. That delegation is never generalized beyond the one decision and is
never inferred from silence.

Preserve true external-authority boundaries. The owner alone appoints or
retires a steward and calls or clears a global halt. Agents do not invent
credentials, spend money, accept legal terms, perform owner-authenticated or
destructive production actions, or communicate externally as the owner. Record
the recommendation, ask once for the missing authority, and continue every
independent part. A vote never overrides an owner prohibition, a repository
safety rule, review independence, a live hold, or a genuinely missing platform
permission.

---

## Identity, review, and holds

Shared GitHub accounts do not identify agents. Your stable identity is your
`agent:<id>` label on the issue and its pull request. Before first use, ensure
no other live lane uses the id. Put `**From:** <your-agent-id>` in claims,
handoffs, decisions, and reviews; never infer the actor from GitHub metadata.

Review a peer's exact head, not your own work. Verify the claim and diff, name
the observable problem and its path, note what you did not check, and withdraw
incorrect findings in writing. Refresh an unreviewed, behind-main PR before
reviewing it. A review can survive a refresh only after checking both the
owned-path diff and the incoming-main blast radius.

Post a verdict as a pull-request review, not an issue comment:

```bash
gh pr review <pr-number> --comment --body "$(printf '**From:** <your-agent-id>\n\n**Verdict:** accept\n')"
```

`**Verdict:**` must be alone on its own line and be `accept`, `accept with
follow-up`, or `changes required`. A shared account may record this as
`COMMENTED`; the exact `From` marker and the lane label are the independence
evidence. Run `gate.sh` after posting it to confirm the gate saw it.

`accept with follow-up` is for genuinely separable work. If the finding is in
the PR's scope or leaves the merged state incomplete, request the change.

Holds are labels, never prose:

| Label | Set by | Cleared by | Meaning |
|---|---|---|---|
| `hold:author` | author | author | an in-progress fix |
| `hold:review:<agent>` | that reviewer | that reviewer | that reviewer's open finding |

Use `hold.sh review add` as soon as a review finding needs a fix, and
`hold.sh review clear` after verifying its resolution. An author never clears a
reviewer's hold. A named review hold may be cleared for an unresponsive holder
only through the steward path in `hold.sh`, with a personally reproduced,
repeatable **verifiable** fact and recorded reason; a judgment call remains the
holder's decision. Fetch a PR head before acting on a review finding.

---

## Heartbeat, stopping, and cleanup

Run an adaptive heartbeat every 10–30 minutes. Each tick starts with
`orient.sh`, drains green independently reviewed unheld PRs, rechecks holds
after author pushes, reviews peers before claiming more work, and continues
your active lane. When no work is actionable, report an honest idle tick. When
the mission finishes or a halt is active, cancel your heartbeat rather than
idling forever.

An open issue labelled `ops:halt` is the global stand-down signal. On a halt,
finish or hand off your lane cleanly, run cleanup, cancel watchers and the
heartbeat, and go silent. A narrow task concern is a task label or hold, not a
global halt.

A merged task still needs cleanup. Delete your merged remote branch explicitly
and use the cleanup script from the appropriate location:

```bash
# Package repository
scripts/cleanup.sh          # dry run
scripts/cleanup.sh --yes

# Adopting repository
docs/ai-team/scripts/cleanup.sh
```

It removes merged local branches and stale worktrees while leaving unmerged or
checked-out work alone. Keep the tree clean; file a separate issue for work
that cannot safely ship in the current lane.

---

## Where things live

| What | Where |
|---|---|
| Tasks and task state | GitHub Issues with `agent:<id>` and `task:*` labels |
| Current work, claims, handoffs, blockers, review findings | The owning issue or pull request |
| Holds | `hold:author`, `hold:review:<agent>`, and `hold.sh` |
| Gates, sweeps, orientation, and cleanup | `scripts/` here; `docs/ai-team/scripts/` in an adopter |
| Halt / stand-down | An open `ops:halt` issue, shown first by `orient.sh` |
| Active steward | One open `ops:steward` issue with one `agent:<id>` label |
| Product and architecture decisions | `decide.sh propose`, `vote`, and `close` on the owning issue |
| External-authority requests | One direct request to the owner, recorded with the affected task |
| Durable architecture decisions | The repository's documented location |

Run `orient.sh` instead of rereading this document. The board is current; this
guide is the smallest stable map for acting on it.
