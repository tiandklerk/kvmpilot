# Contributing

This repo follows the team's internal **engineering ways of working** standard
(on the team wiki). The short version:

## Never commit to `main`

Every change — however small — goes through a **branch and a PR**. No direct
commits, no force-push to `main`. This is enforced server-side (GitHub branch
protection) since the repo is public, and locally by the hook below.

- Branch off the latest `main`, named by intent: `feat/…`, `fix/…`, `chore/…`,
  `docs/…`, `test/…`, `refactor/…`, `security/…`.
- **Conventional commits:** `type(scope): summary` (imperative, ≤ ~72 chars).
  Body explains *why*. Don't commit secrets or build artifacts.
- **One logical change per PR.** The description says *what* changed, *why*, *how
  it was verified*, and *residual risk*. Behaviour changes get tests; a security
  fix gets a regression test where feasible.
- **Agents open PRs; a human reviews and merges.** No self-merge, no auto-merge,
  no deploying.

## Enable the local branch-protection hook

```bash
git config core.hooksPath hooks
```

The `hooks/pre-push` hook refuses a direct push to `main` from your clone — the
local first line of defence, in addition to the server-side protection.

## Tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest        # runs against the mock kvmd device; no PiKVM needed
```
