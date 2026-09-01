# AI-team mechanisms

These scripts enforce the reusable operating guide beside them in
`../README.md`. Adopting repositories mount the whole package at
`docs/ai-team/` with `git subtree` rather than copying it by hand — see "Where
this directory lives, and why" in the guide.

Most scripts are repository-independent and resolve the current GitHub
repository from `origin`, not from ambient `gh` state (#445/#37) — a checkout
whose `gh` resolves elsewhere still targets `origin`'s repository. The project
hook is the deliberate exception: it is
local source pins, assembly checks, and project commands, and it lives outside
this mount entirely — at `.ai-team/project-orient.sh` in the adopting
repository's own root, copied from `../templates/project-orient.sh` — so it is
never part of what `git subtree pull` touches.

The package ships `templates/mechanisms.yml`,
`templates/blocked-by-sweep.yml`, and `templates/independent-review.yml` for
adopters to copy into their own `.github/workflows/` directory at mount time.
GitHub owns the triggers and permissions there: the mechanism workflow runs
the mounted suite unfiltered on pull requests, the schedule-only sweep is the
only job granted `issues: write`, and the `pull_request_target`-only
independent-review workflow fetches the grammar through the Contents API at the
exact trusted `github.workflow_sha`. It does not check out repository code and
fails if the canonical grammar path is absent or malformed. The implementation
and its tests live here, with the board contract they enforce.

Because review events are not trusted triggers, submit or update the review and
then rerun the latest failed `Independent review` run for the current head; a
label transition also starts a new target run. For a fresh install, land the
mount and workflow together before requiring the check. For an existing
adopter, retain a fail-closed precursor against whichever trusted grammar path
it already has (or stage a standalone grammar at a custom trusted path) until
the transition pull request has both installed the mounted grammar at
`docs/ai-team/scripts/review_gate.sh` and copied
`docs/ai-team/templates/independent-review.yml` into the adopter's root
workflow directory. A 404 is never a successful installation signal.

The package also ships `templates/activate.sh` and
`templates/package-refresh.conf`. Copy both to the adopter-owned `.ai-team/`
directory and enter through `.ai-team/activate.sh`; it updates the mount in an
isolated PR before handing off to `docs/ai-team/scripts/orient.sh`. The
operating guide's “Activate and refresh the mount” section is mandatory for a
legacy first migration: old claim clients do not participate in the new
remote-ref mutex, so the owner must establish the documented exclusive window
rather than treating the acknowledgement variable as a lock.

`blocked_by_sweep.py` is a Python entry point, not a shell command. Run it as
`python3 docs/ai-team/scripts/blocked_by_sweep.py` (the workflow supplies the
required `GITHUB_REPOSITORY` and `GITHUB_TOKEN` environment variables).

`land.sh` is the terminal lane transition. After an independent review, run
`LAND_AGENT=<stable-agent-id> land.sh <pr> <reviewed-full-sha>`; it runs the gate,
merges through the REST endpoint only when the gate passes, moves the PR and its
issue to `task:done`, and records the acting agent. A rerun after a transient
post-merge failure retries terminalization without attempting a second merge.

`review_gate.sh`, `gate.sh`, and `land.sh` share one fail-closed trusted-author
predicate for same-repository Dependabot PRs. It is anchored to Dependabot's
immutable REST account id and corroborates the bot type and login; display
metadata, branch names, labels, and `github.actor` never grant the exception.
An open trusted PR must have no `agent:*` or `task:*` labels, still needs a
distinct exact-head acceptance, and remains subject to holds and every ordinary
check. The canonical review body must contain one `**From:** <reviewer>` line
and one `**HEAD reviewed:** <full-sha>` line; an approval or standalone
`**Verdict:** accept` supplies the verdict. Both that explicit marker and the
GitHub API `commit_id` must equal the current head. The marker is decisive
because GitHub can rewrite an older Dependabot review's `commit_id` during a
rebase while its body still names the old head. The lane is issue-less; landing
adds `task:done` only to the PR. Adopters need only update the mounted
package—permissions and workflow configuration stay the same—but must repost
marker-less reviews and retrigger Independent review for accepted bot PRs after
update.

`label_hygiene.sh` is called by `orient.sh` and also accepts a repository name
directly. It reports open task-label contradictions and non-terminal labels on
issues closed within `AI_TEAM_CLOSED_HYGIENE_DAYS` (30 by default).

`hold.sh` sets or clears a named review hold — `CLAIM_AGENT=<id> hold.sh
review add|clear <pr>` applies or removes `hold:review:<id>`, creating the
label on first use the same way `claim.sh` creates `agent:<id>` labels. Each
holder's label is independent (#385): `gate.sh` blocks on every one present,
and clearing yours never touches another reviewer's.

A steward clearing an unresponsive holder's demonstrably factual finding must
declare and record the classification: `hold.sh review clear <pr> --steward
<holder> --discharge verifiable --reason "<reproducible evidence>"`. The script
refuses `--discharge judgment`; a design or trade-off decision remains the
holder's named finding until that holder records a verdict.

## Posting and reading the board

**Posts without the machine header are invisible to team tooling.** That one
sentence is the whole policy; `board.sh` is its mechanism (#363):

```bash
board.sh post 361 --agent fable --type status "pushed the fix, head is 4f816d5f"
board.sh digest 361     # headered posts, PR review verdicts, and unheadered
                        # human posts (they may be the owner); bot noise and
                        # <details> folds skipped
board.sh hygiene        # unstructured-post counts on active lanes (orient.sh runs this)
```

`post` stamps the `**From:**` header `gate.sh` parses, folds anything over the
visible-byte budget (`BOARD_POST_BUDGET`, default 1400) into a `<details>`
block, and **refuses `--type verdict`** — a verdict posted as an issue comment
is invisible to the gate (#359); record verdicts as PR reviews. `digest` is the
sanctioned way to catch up on a thread: it merges the conversation stream with
the PR **review** stream (where verdicts live — the #359 split), renders
headered posts and unheadered human-account posts alike (an unheadered human
post may be the owner, whose rulings outrank every marker; only bot posts are
hidden), and strips the folds — so reading it costs a fraction of the raw
thread without hiding anything that can bind you.

## Running the mechanism tests

Run them from the repository root. In this repository the scripts sit at
`package/scripts/`; in an adopting repository they sit at `docs/ai-team/scripts/`.

```bash
# Linux / macOS — here
python3 -m unittest discover -s package/scripts -p 'test_*.py'

# Linux / macOS — in an adopting repository
python3 -m unittest discover -s docs/ai-team/scripts -p 'test_*.py'

# Windows (PowerShell or Git Bash — the harness resolves Git for Windows' Bash;
# use `python` because `python3` may be the Store alias that exits immediately)
# — here
python -m unittest discover -s package/scripts -p 'test_*.py'
# — in an adopting repository
python -m unittest discover -s docs/ai-team/scripts -p 'test_*.py'
```

They are hermetic — stubbed `gh`, a `git` shim for the origin-identity answer,
and local bare repositories instead of the network. Every adopting repository
should run them as a required check, so that a gate or sweep regression fails
before it reaches a merge.
