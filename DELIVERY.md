# Delivery cycle

Use this alongside README.md and the installed scripts. The objective is a
completed, verified user outcome with low end-to-end delay, not a PR quota.
No step here waives independent review, required CI, holds, owner halts or
security boundaries. Keep evidence on the existing issue/PR.

## Select and claim

Finish owned review-ready work and its actionable failures before adding work.
Default to one authored lane per agent per repository, including subagents
sharing that identity. `claim.sh` refuses another open owned PR, including a
half-claim with a From marker; it still resumes existing claims so a backlog
can drain. This is a live preflight, not a distributed lock: serialize claim
calls sharing an identity. Never bypass it with slot paths or manual labels.
An owner-approved disjoint exception must name scope, writer and reviewer
capacity on the issue; it does not override a script refusal. Use explicit
handoff/ownership disposition rather than silently exceeding the installed
limit. Independent read-only investigation or peer review can use CI wait time.

Verify prerequisites on the actual base before marking a task ready or claiming
it. Record unresolved `Blocked-By` references; a ready label is not proof that
a dependency landed. Sequence changes to shared contracts before their callers
and agree path ownership before concurrent work.

## Implement and verify

State the changed role, entry point, action and expected visible result for UI
work. Run tests appropriate to the delta and cheap relevant integration checks
before review (for example action coverage, shared test-helper collisions or
PostgreSQL behavior). Keep required full CI; do not repeat whole suites solely
because CI is pending.

Reuse current browser end-to-end evidence when it covers the changed role and
journey. Only an uncovered changed UI path needs a short agent-run smoke check
using synthetic data: navigate to it, perform the action, check feedback and
persistence. Check relevant denied-role, empty or prerequisite states when they
changed. Apply the project's design and existing component/table standards to
the touched surface. Backend, documentation and CI-only changes use their
appropriate tests, without a browser ceremony.

Batch related checks in a coherent journey/session rather than repeat login
and setup per tiny PR. Link the actual tested revision and covered actions;
previous evidence does not prove subsequently changed behavior. No whole-app
regression audit, video, live owner demo or new approval gate is required.
Record a specific unavailable environment or untested path for the existing
reviewer; never invent a pass. Consequential uncovered risk is resolved in the
existing review, not hidden by marking a smoke check complete.

## Ready and review

Before `ready.sh`, record outcome, revision, relevant role, tests/browser result,
elapsed verification minutes and meaningful unchecked limits in the PR. Keep
this short; link existing evidence instead of duplicating logs.

An independent reviewer checks the claim, changed invariants and evidence.
Prioritize authorization, tenancy and data loss; use targeted mutation probes
where they prove protection, not a mandatory deletion of every mechanical
branch. Reuse applicable author evidence after checking its relevance, and
reproduce risky or disputed results. UI review checks journey evidence and
consequential gaps without automatically repeating the author's whole session.

Use the installed exact-head review contract. Reviewer-bound failing tests may
clear through CI only with the documented finding/HEAD/API commit binding and
immediate child fix. Semantic conflict resolutions, further commits or
untestable findings require the existing reviewer's corrected-head verdict.
Do not demand a redundant acceptance when the gate proves valid clearance or
pure-merge carry-forward, or assume that any green test clears any finding.

## Handoff and land

On ready, verdict, addressed finding or observed CI completion, use one
supported direct handoff to the responsible reachable agent with PR, current
head and next action. Keep the durable evidence on GitHub. A message is a wakeup,
not authority to merge: recheck the current head, halt, holds and `gate.sh`, then
use `land.sh`. Rerun the trusted review check once after a review changes; do
not repeatedly replay the same run while nothing changed.

The dependency-sweep template wakes on issue closure and checks out the default
branch. It never runs pull-request code or publishes an independent-review
verdict. Its schedule remains the cross-repository/missed-event backstop.
Adopters must copy the updated template to their owned workflow in a reviewed
mount update; changing the package alone does not update installed triggers.

Use supported completion waits where available. Otherwise return to the existing
heartbeat; do not create short polling loops or a new orchestrator. Targeted
wakeups inspect that lane; heartbeat orientation covers owned/actionable work
and expands to other boards when selecting work or covering stewardship. Reuse
one tick's snapshot, but recheck safety facts before mutation. Stay quiet when
unchanged; do not manufacture backlog to meet a queue-count target.

## Checkpoint and measure

Completion/handoff checklist, on the existing issue or PR:

- Owner records objective/acceptance criteria, decisions and constraints,
  repository/branch, PR/current revision, tests/review status, blockers and exact
  next action, with evidence pointers instead of transcript copies.
- Commit/push intended work normally and identify unsaved work explicitly;
  never clear context over active tools or edits.
- Before assigning fresh-session follow-up, the coordinator reads the checkpoint,
  checks for an existing worker/claim, and preserves issue/PR ownership until
  explicit handoff. Do not start a duplicate worker or claim.
- On resume, revalidate repository/head, CI/reviews, holds and halt. Continue
  the existing issue/PR; stale checkpoint facts do not authorize mutations.

Prefer a fresh task at a safe boundary only when the runtime supports it and
the owner authorizes the rotation. Verify delivery of the checkpoint and any
heartbeat retarget before retiring the old task. Automatic context compaction
is runtime behavior, not a package command; there is no timer-based context
reset here. A steward that can spawn subagents cannot necessarily create a
top-level session: use checkpoints and any native supported compaction in the
existing session; fresh-session rotation needs verified harness or operator
support. This is documented coordination, not automated rotation; no new
scheduler or automatic task creation is implemented. Avoid carrying entire
transcripts or repeatedly rereading unchanged
boards and documentation. Reuse compact checkpoints with fresh live facts.

Periodically sample completed journeys, reopened defects, claim-to-ready,
ready-to-first-verdict and ready-to-land latency, CI retries and verification
minutes from existing PR evidence and GitHub timestamps. State window and sample;
distinguish elapsed waiting from active work. Compare verification effort with
escaped/reopened UI defects to judge its net benefit. PR count is context, not
a quota; do not split work to inflate it. No per-tick metrics report or new
telemetry service. Do not infer money from elapsed time or cached token totals.
