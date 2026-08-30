# AI-team mechanisms

These scripts enforce the reusable operating guide beside them in
`../README.md`. Adopting repositories mount the whole package at
`docs/ai-team/` with `git subtree` rather than copying it by hand — see "Where
this directory lives, and why" in the guide.

Most scripts are repository-independent and resolve the current GitHub
repository through `gh`. The project hook is the deliberate exception: it is
local source pins, assembly checks, and project commands, and it lives outside
this mount entirely — at `.ai-team/project-orient.sh` in the adopting
repository's own root, copied from `../templates/project-orient.sh` — so it is
never part of what `git subtree pull` touches.

The package ships `templates/mechanisms.yml`,
`templates/blocked-by-sweep.yml`, and `templates/independent-review.yml` for
adopters to copy into their own `.github/workflows/` directory at mount time.
GitHub owns the triggers and permissions there: the mechanism workflow runs
the mounted suite unfiltered on pull requests, the schedule-only sweep is the
only job granted `issues: write`, and the independent-review workflow reads
the trusted default-branch grammar. The implementation and its tests live
here, with the board contract they enforce.

`blocked_by_sweep.py` is a Python entry point, not a shell command. Run it as
`python3 docs/ai-team/scripts/blocked_by_sweep.py` (the workflow supplies the
required `GITHUB_REPOSITORY` and `GITHUB_TOKEN` environment variables).

`land.sh` is the terminal lane transition. After an independent review, run
`LAND_AGENT=<stable-agent-id> land.sh <pr> <reviewed-full-sha>`; it runs the gate,
merges through the REST endpoint only when the gate passes, moves the PR and its
issue to `task:done`, and records the acting agent. A rerun after a transient
post-merge failure retries terminalization without attempting a second merge.

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
`scripts/`; in an adopting repository they sit at `docs/ai-team/scripts/`.

```bash
# Linux / macOS — here
python3 -m unittest discover -s scripts -p 'test_*.py'

# Linux / macOS — in an adopting repository
python3 -m unittest discover -s docs/ai-team/scripts -p 'test_*.py'

# Windows (PowerShell or Git Bash — the harness resolves Git for Windows' Bash;
# use `python` because `python3` may be the Store alias that exits immediately)
python -m unittest discover -s scripts -p 'test_*.py'
```

They are hermetic — stubbed `gh`, a `git` shim for the origin-identity answer,
and local bare repositories instead of the network. Every adopting repository
should run them as a required check, so that a gate or sweep regression fails
before it reaches a merge.
