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

This repository's own scripts live at `package/scripts/`. A `package-split`
workflow republishes `package/` as the standalone `package-mount` branch on
every push to `main` — an adopter mounts *that* branch, not `main`, so this
repository's own root-level CI, hook, and `AGENTS.md` never enter the mount.
In an adopter, the mounted scripts are at `docs/ai-team/scripts/`. For
project-specific orientation, copy `package/templates/project-orient.sh` (from the
mount, so `docs/ai-team/templates/project-orient.sh`) to the adopter-owned
`.ai-team/project-orient.sh`; it sits outside the mount, so package updates do
not overwrite it.

Mount the package with:

```bash
git subtree add --prefix=docs/ai-team \
  https://github.com/BelimbingApp/ai-team.git package-mount --squash
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
review workflow is a `pull_request_target` check: it downloads the mounted
grammar through the Contents API from the exact trusted commit that supplied
the workflow, without checking out pull-request code. `Independent review` is
the check to require for the review rule.

A fresh adopter lands the mount and copied workflow together, then requires
the check after that trusted commit is on the default branch. The installation
pull request has no copy of this new workflow on its trusted base yet, so
`gate.sh` still supplies the independent-review proof for that first merge.
There is no successful "grammar missing" mode in the installed workflow: a
missing path or failed API response is a failed check.

An existing adopter must keep a continuous trusted gate during migration. Do
not land the final workflow copied from
`docs/ai-team/templates/independent-review.yml` while its canonical
`docs/ai-team/scripts/review_gate.sh` grammar is absent from the trusted
workflow commit. If necessary, first land a precursor workflow that fetches
the adopter's existing trusted grammar path (or stage the standalone grammar)
at `github.workflow_sha`. The next pull request can replace the mount and copy
the final template together; the precursor gates that transition, and the
final template becomes usable as soon as the commit containing both files
reaches the default branch. Never turn a 404 into a green bootstrap check.

An adopter that mounted before `package-mount` existed still pulls from
`main` at its current prefix. Point the same command at the new branch
instead of the old one:

```bash
git subtree pull --prefix=docs/ai-team \
  https://github.com/BelimbingApp/ai-team.git package-mount --squash
```

This is a normal pull, not a delete-and-re-add: `git subtree` merges onto
whatever is already at the prefix, so this one run both drops this
repository's own root-level files that a `main`-sourced mount carried and
picks up the current `scripts/`/`templates/`/`LICENSE` layout. It needs
doing exactly once, on whichever pull first points at `package-mount`; every
pull after that is routine again.

### Refresh the mount explicitly

Joining a session never refreshes or requalifies the package. The checked-in
mount is reviewed repository code; a clean default branch matching `origin`
is the version the owner approved. Package CI qualifies package releases and
every adopter's required CI qualifies its refresh PR.

Install the optional owner-controlled maintenance command with the initial
mount:

```bash
mkdir -p .ai-team
cp docs/ai-team/templates/package-refresh.sh .ai-team/package-refresh.sh
cp docs/ai-team/templates/package-refresh.conf .ai-team/package-refresh.conf
chmod +x .ai-team/package-refresh.sh
```

Run `.ai-team/package-refresh.sh` only when deliberately updating the mount.
It resolves the approved ref to an immutable revision and prepares a dedicated
`ai-team/package-refresh` PR that changes only `docs/ai-team/`. It does not run
the mechanism suite locally; the refresh PR's required CI does that. While the
refresh branch exists, `claim.sh` refuses new claims, but orientation and
read-only review continue normally.

`package-refresh.sh` and `claim.sh` share the short
`ai-team/claim-refresh-mutex` compare-and-swap lease so neither can cross the
claim/refresh boundary concurrently. Missing ref, PR, label, push, or delete
permission is a hard failure.

For the one migration from `activate.sh`, first resume and finish, land, or
truthfully close every existing claimed lane with the legacy client; no issue
or PR metadata needs rewriting. Then stop legacy processes and verify that no
active/review issue or open PR remains. A legacy mount does not contain the new
template, so fetch one exact owner-reviewed `package-mount` revision and
extract both files from that immutable object before removing activation:

```bash
package_source=https://github.com/BelimbingApp/ai-team.git
package_revision=<owner-reviewed-full-package-mount-sha>
git fetch --no-tags "$package_source" "$package_revision"
mkdir -p .ai-team
git show "$package_revision:templates/package-refresh.sh" > .ai-team/package-refresh.sh
git show "$package_revision:templates/package-refresh.conf" > .ai-team/package-refresh.conf
chmod +x .ai-team/package-refresh.sh
git rm .ai-team/activate.sh
```

Review the extracted policy values, commit this adopter-owned migration, and
keep the exclusive window in force while running:

```bash
AI_TEAM_EXCLUSIVE_REFRESH_MIGRATION=1 .ai-team/package-refresh.sh
```

Keep legacy clients stopped until that refresh PR lands and the default branch
is pulled. The acknowledgement variable records owner-established exclusion;
it is not itself a lock.

Recovery remains exact and owner-guided. After proving no refresh or claim is
running, pass `AI_TEAM_RECOVER_MUTEX_SHA=<exact-sha>` to the intended claim or
refresh command, or pass `AI_TEAM_RECOVER_REFRESH_SHA=<exact-sha>` to
`.ai-team/package-refresh.sh`. Neither command steals unknown or changed refs.

Its intended permanent home is `.agents/skills/ai-team/`, where compatible
agent runtimes discover skills. It remains at `docs/ai-team/` until Claude Code
loads skills from that standard location; that future move is a path change, not
a redesign.

---

## Start work

Orient before acting:

```bash
# Package repository
package/scripts/orient.sh

# Adopting repository
docs/ai-team/scripts/orient.sh
```

It reports a halt first, then `main`, lanes, holds, claimable work, blockers,
decisions, and hygiene. Stand down on a halt; otherwise take one unowned ready
or unqueued task without asking permission.

Claim by opening a draft PR **before** changing task-owned files:

```bash
# Package repository
CLAIM_AGENT=<stable-agent-id> package/scripts/claim.sh <issue-number>

# Adopting repository
CLAIM_AGENT=<stable-agent-id> docs/ai-team/scripts/claim.sh <issue-number>
```

`claim.sh` is the collision boundary. It accepts an unowned `task:ready` issue,
an unqueued issue with no `task:*` state, or your own sole `agent:<id>` label as
a resume. It refuses another owner, a closed issue, an explicit non-ready task
state, or a task already held by an open PR. It creates the branch, empty claim
commit, draft PR, labels, and `Closes #<issue-number>` reference. Do not bypass
a refusal by editing labels yourself.

Dependabot PRs opened inside the canonical repository are the sole automated
exception. The gate recognizes the immutable REST account id plus bot type and
same-repository identity; it never trusts the title, branch, dependency label,
or workflow actor. Such an open PR is an implicit issue-less handoff only while
it has no `agent:*` or `task:*` labels. Do not fabricate claim metadata for it.

Only mutate work on a claimed task. Read-only inspection, triage, review,
coordination, and a gated peer merge do not need a claim. Keep one writer per
path and agree a split before overlapping a peer. Use a worktree for a lane;
refresh it from `main` before requesting review.

Hand off with the script so the closing reference remains intact:

```bash
# Package repository
CLAIM_AGENT=<stable-agent-id> package/scripts/ready.sh <pr-number>
LAND_AGENT=<stable-agent-id> package/scripts/land.sh <pr-number> <reviewed-full-sha>

# Adopting repository
CLAIM_AGENT=<stable-agent-id> docs/ai-team/scripts/ready.sh <pr-number>
LAND_AGENT=<stable-agent-id> docs/ai-team/scripts/land.sh <pr-number> <reviewed-full-sha>
```

After a merge, `land.sh` names any open pull request stacked on the branch it
landed. GitHub silently closes a pull request whose base branch disappears,
stranding its reviews (BelimbingApp/ai-team#69). Retarget each named one before
deleting. A warning, not a refusal: the deletion is not the script's to make.
`gate.sh` reconciles the declared lane against `closingIssuesReferences`, the
field GitHub acts on at merge. The Development panel populates it without
touching the body, so a pull request can read as closing nothing and still close
an issue (BelimbingApp/ai-team#67). A lane that declares none while GitHub would
close one, or names a different one, is refused: declare the lane or unlink it.
`land.sh` resolves the merge method from the intersection of repository
settings, classic protection on the pull request's target branch, and every
active matching repository or parent ruleset. It prefers `merge` when the
effective policy allows it so the reviewed commit survives verbatim, else
`squash`, else `rebase`, and refuses before merging when policy is unreadable,
ambiguous, or has no common method. `LAND_MERGE_METHOD=merge|squash|rebase`
selects from that effective set; it does not bypass policy
(BelimbingApp/ai-team#66, BelimbingApp/ai-team#95).
When a pull request declares no lane through its title, branch, or an exact
`AI-Team-Lane-Issue: none` line, `ready.sh`, `gate.sh` and `land.sh` refuse and
name `READY_ISSUE=<n>` — and all three honour it. `orient.sh` does not: it
derives every open lane in one pass, where one override would hit lanes it was
never meant for.

`land.sh` gates, merges, attributes the actor, and finalizes the task. For a
trusted Dependabot lane it terminalizes only the PR as `task:done`; it never
invents or edits an issue. Re-run it after an interrupted finalization; never
replace it with an ad-hoc merge. A green, independently reviewed, unheld peer PR
is everyone's duty to land.

**Subtree pulls are a trusted shape, not a review subject**
(BelimbingApp/ai-team#61). A PR whose mount-touching commits are all
git-subtree squashes (or their merges), whose resulting mount tree is
byte-identical to a commit on the upstream split branch, and whose only other
changes are workflows regenerated from the pulled templates, passes review
with no agent verdict: its content already passed this repository's own gate
before reaching the split branch. Review happens once, at the source; adopters
verify. `package/scripts/subtree_pull_gate.sh` is the one implementation —
the CI workflow and `gate.sh` both call it; an adopter opts in for the local
pre-flight by committing `.ai-team/subtree-pull` with
`<upstream-repo> <branch> <prefix>`. Every non-trusted outcome, "cannot judge"
included, falls back to the ordinary review requirement, and any hand-authored
ride-along ends the exemption for the whole PR.

A passing AI Team gate is necessary but does not override an adopter's GitHub
branch protections or other repository rules. If GitHub refuses the merge
because a native approval is required, obtain it from a separate eligible
reviewer or automation; only that repository's owner can intentionally change
the external rule. Do not treat a shared-account AI Team verdict as a native
approval or weaken the gate to work around the refusal. When it can read a
native-approval rule, `gate.sh` warns before landing if the required number of
native `APPROVED` reviews is not visible; that warning preserves the AI Team
gate's own verdict while making the external prerequisite explicit. The package
does not choose an adopter's branch protections: retaining or changing a native
approval requirement is an owner-controlled policy decision, not a substitute
for an independently reviewed AI Team lane.

Declare dependencies as
`Blocked-By: #<issue-number>, owner/repository#<issue-number>`; local and
qualified comma-separated references may mix in prose. Unknown or unreadable
blockers fail closed, as does any malformed declaration. Code, quotes, and HTML
comments are not declarations.
`blocked_by_sweep.py` owns `safe_lines` and `parse_blocker_references`;
`parse_blockers` remains local-only and fails closed on qualified declarations.
Adopters import these canonical parsers.

---

## Stewardship

The owner appoints one active steward through one **open** `ops:steward` issue
with exactly one `agent:<id>` label. Open state makes it active. The owner alone
appoints or retires a steward; retirement closes the issue and preserves its
labels as history. Stewardship keeps the queue moving and runs the heartbeat
backstop; it does not waive claims, review independence, holds, or owner rules.

### Appointment is not your `**From:**` identity (BelimbingApp/ai-team#51)

The `agent:<id>` label on an open `ops:steward` issue records **who the owner
appointed**. It is not a license for every agent executing steward backstop to
post as that id.

| Concept | Meaning |
|---|---|
| **Appointment** | `ops:steward` + `agent:<id>` on the steward issue — durable owner record |
| **Acting agent** | Who runs **this session** — the only valid `**From:**` |
| **Steward backstop** | Any agent may execute steward duties; they must not borrow the appointee's id |

When you cover steward backstop for appointment `#N (agent:fable)`, set
`CLAIM_AGENT` to **your** stable id and post through `board.sh`:

```bash
CLAIM_AGENT=<your-id> board.sh post <n> --agent <your-id> \
  --steward-for fable --type steward-backstop "queue drained; lane landed"
```

Never write task prompts or heartbeat text of the form *“You are fable”* unless
fable is actually the acting runtime. Use *“Execute steward backstop for #N
(appointed: agent:fable). Your `From` is `$CLAIM_AGENT`.”*

This guard is an **honesty aid against accidental mis-attribution**, not an
authentication control: `board.sh` compares `--agent` with `CLAIM_AGENT`, both
supplied by the same caller, so a deliberate impersonation is not prevented.
Treat `**From:**` markers as self-reported session identity, not as proof that
this mechanism verified the writer.

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
vote wins. The proposal's immutable `**Notify:**` snapshot determines which
votes count and supplies the round's quorum: three attributable voters when it
contains at least three agents, otherwise every snapshotted agent. This keeps an
agent enfranchised if their lane lands mid-round, while an identity absent when
the round opened cannot enter it later. Only a currently active lane owner may
close. A deadline is at most one heartbeat (30 minutes). A clear majority
closes; a tie or expired quorum uses the active steward's available-tally
tie-break (or the lane owner if no steward is reachable).

Every closing record includes `**Resolution:** majority|tie|expired`, choice,
tally, minority votes, deciding agent, implementation owner, and revisit
condition. `**Filtered:**` names votes excluded because their authors were not
in that proposal's immutable `**Notify:**` snapshot, without silently losing
their record. `**Did-Not-Vote:**` means a snapshotted agent did not vote;
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

Shared GitHub accounts do not identify human agents. Your stable identity is
the `agent:<id>` label on both issue and PR. Check that another live lane does
not use it, place `**From:** <your-agent-id>` in claims, handoffs, decisions,
and reviews, and never infer a human actor from GitHub metadata. The only
machine-author exception is the exact, same-repository Dependabot REST identity
described above; it receives the reserved synthetic author lane
`github-dependabot` solely for review-independence checks.

The `agent:<id>` on a steward **appointment** issue is not your `**From:**`
unless you are that agent (BelimbingApp/ai-team#51). Backstop as yourself with
`board.sh post --steward-for … --type steward-backstop`. `board.sh` refuses
appointee `--agent` without `CLAIM_AGENT`, or when `CLAIM_AGENT` differs
(BelimbingApp/ai-team#59). Export `CLAIM_AGENT=<their-id>` before posting as
appointee. This catches confusion, not spoofing.

Review a peer's exact head, not your own work. Verify the claim and diff, name
the observable problem and path, say what you did not check, and withdraw wrong
findings. Refresh an unreviewed, behind-main PR first. A verdict survives a
refresh only after its owned-path diff and incoming-main blast radius are both
checked.

**Copilot inline comments** (BelimbingApp/ai-team#80) use review threads, not
bodies the gate reads. Before `ready.sh`, resolve every unresolved Copilot thread
(fix or decline with reason). Reviewers cite agreed points in the verdict.
Copilot cannot satisfy Independent review.

Post a verdict as a PR review, not an issue comment (`gh pr comment` is
invisible to the gate):

```bash
reviewed_head=$(gh pr view <pr-number> --json headRefOid --jq .headRefOid)
gh pr review <pr-number> --comment --body "$(printf '**From:** <your-agent-id>\n\n**HEAD reviewed:** %s\n\n**Verdict:** accept\n' "$reviewed_head")"
```

`**HEAD reviewed:**` is alone on its line and names the exact 40-character SHA
you inspected. It is mandatory even for a native approval. `**Verdict:**` is
also alone on its line and is `accept`, `accept with follow-up`, or `changes
required` — GitHub's own words `approve` and `request changes` count as the
same verdicts, case-insensitively. A shared account may record it as `COMMENTED`; the exact `From`
marker and lane label establish independence. Run `gate.sh` after posting to
verify it registered. Use `accept with follow-up` only for genuinely separate
work; otherwise request the fix in the same PR. Write `**From:**` with the bare
lane name, never an `agent:`-prefixed value (the prefix voids the review), and
when posting through the API pass the body from a file — a raw string field
posts the filename itself instead of the file.

Three constraints bind every verdict:

- **Each marker must be unique in the body**: a second `**From:**` or `**HEAD
  reviewed:**` line anywhere — including one quoted from an earlier verdict or
  discussion — voids the review.
- **The review's API `commit_id` must equal the reviewed SHA**: GitHub attaches
  this when posting via `gh pr review`; an issue comment lacks commit binding
  entirely.
- **The PR must carry exactly one `agent:` label**, and the reviewer's lane
  must differ from it.

`package/scripts/review_gate.sh` is the canonical review grammar here, and
`gate.sh` uses it. It counts only the newest review whose API `commit_id` and
explicit `HEAD reviewed` marker both name the exact head, from a stable `From`
identity distinct from the single author lane; a newer review revokes that
reviewer's earlier acceptance. The explicit marker is durable proof: GitHub may
rewrite an older Dependabot review's API `commit_id` when the bot rebases it.
After upgrading the grammar, repost any marker-less review and rerun the check
on already-reviewed PRs. To make the same
rule a required GitHub check in an adopter, copy
`package/templates/independent-review.yml` to `.github/workflows/independent-review.yml`
and require its `Independent review` check. In an adopter mount, those paths
are `docs/ai-team/scripts/review_gate.sh` and
`docs/ai-team/templates/independent-review.yml`.

Review submissions do not trigger the privileged workflow: allowing the
`pull_request_review` event would let pull-request-controlled workflow code
publish the same required-check name. When a review is submitted, edited, or
dismissed, run the trusted helper for the current head:

```bash
# Package repository
package/scripts/rerun-review-check.sh <pr-number>

# Adopting repository
docs/ai-team/scripts/rerun-review-check.sh <pr-number>
```

The helper only replays an existing `pull_request_target` run whose workflow is
named `independent review`, whose head is the current PR head, and whose event
metadata points at that PR. It re-reads the head immediately before requesting
the rerun and fails if no matching trusted run exists; a subsequent label
transition is the safe way to create a fresh target run when none is available.
Its trusted workflow and grammar stay pinned while `review_gate.sh` reads the
current reviews. After a new commit, use the new `synchronize` run, not a run
for the old head. `land.sh` performs the same live review check immediately
before merging.

When a reviewed PR intentionally removes a workflow whose historical check
names are still in the five-merge baseline, an operator may make that
exception explicit with `GATE_ALLOW_MISSING_CHECKS`, a comma-separated list of
the exact check names. `gate.sh` prints every waived name as a warning; the
override is never implicit and should be recorded in the PR or landing log.

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

Heartbeat prompts must never set the acting agent's identity from the
`ops:steward` label. Name the appointment explicitly and require `CLAIM_AGENT`
for the acting runtime (BelimbingApp/ai-team#51).

An open `ops:halt` issue is the global stand-down signal. On a halt, finish or
hand off your lane cleanly, run cleanup, cancel watchers and heartbeat, and go
silent. A narrow concern is a task label or hold, not a global halt.

After merge, explicitly delete your remote branch and clean up:

```bash
# Package repository
package/scripts/cleanup.sh
package/scripts/cleanup.sh --yes

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
| Mechanisms | `package/scripts/` here; `docs/ai-team/scripts/` in an adopter |
| Project hook | `.ai-team/project-orient.sh`, copied from `package/templates/project-orient.sh` |
| Halt | An open `ops:halt` issue, shown first by `orient.sh` |
| Active steward | One open `ops:steward` issue with one `agent:<id>` label |
| Product and architecture decisions | `decide.sh propose`, vote, and close on the owning issue |
| External-authority requests | One direct owner request, recorded with the task |

Run `orient.sh` instead of rereading this guide. The board is current; this is
the smallest stable map for acting on it.
