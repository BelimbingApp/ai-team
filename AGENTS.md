# Working in this repository

`README.md` is the operating guide, and it applies here first: this repository
is the package other repositories mount at `docs/ai-team/`, so the team that
maintains it works the way the guide describes. Read it, then run
`scripts/orient.sh` to see the board.

Two things are specific to this repository:

- **Every change ships to adopters.** A mechanism change changes how other
  teams work. Land it with a test in the same PR — `python3 -m unittest
  discover -s scripts -p 'test_*.py'` — and say in the PR body what an adopting
  repository has to do differently after the merge, or that nothing changes.
- **Paths differ from where the package runs.** Here the scripts are at
  `scripts/`; in an adopting repository they are at `docs/ai-team/scripts/`.
  Comments and documentation that name a path should say which one they mean.
