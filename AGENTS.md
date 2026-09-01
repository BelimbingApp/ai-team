# Working in this repository

`package/README.md` is the operating guide, and it applies here first: this
repository is the package other repositories mount at `docs/ai-team/`, so the
team that maintains it works the way the guide describes. It lives under
`package/` rather than at the repository root because it ships to every
adopter as `docs/ai-team/README.md` — the same reason `package/LICENSE` does.
Read it, then run `package/scripts/orient.sh` to see the board.

Two things are specific to this repository:

- **Every change ships to adopters.** A mechanism change changes how other
  teams work. Land it with a test in the same PR — `python3 -m unittest
  discover -s package/scripts -p 'test_*.py'` — and say in the PR body what an
  adopting repository has to do differently after the merge, or that nothing
  changes.
- **Paths differ from where the package runs.** Here the scripts are at
  `package/scripts/` (the repository root also carries this repository's own
  CI and hook, which the `package/` split keeps out of the mount — #26); in
  an adopting repository the scripts are at `docs/ai-team/scripts/`. Comments
  and documentation that name a path should say which one they mean.
- **Appointment is not identity (#51).** The `agent:<id>` on an open
  `ops:steward` issue names who the owner appointed; it is not the acting
  agent's `**From:**` unless that agent is actually running the session. Set
  `CLAIM_AGENT` to your stable id; use `board.sh post --steward-for` for
  substitute backstop.
