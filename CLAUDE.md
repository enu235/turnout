# Working in this repo

Turnout: a local model router with a **swappable routing policy** as its defining feature. Read
`docs/architecture.md` before making structural changes.

## The rule that matters

Three concerns are deliberately kept apart, and mixing them is the one thing that
breaks this design:

- **Router** (`turnout/routers/`) decides WHICH target. It must stay pure:
  no credentials, no subprocesses, no retries, no writes.
- **Adapter** (`turnout/adapters/`) knows HOW to reach one provider.
  It has no opinion about which model should have been chosen.
- **Executor** (`turnout/executor.py`) runs the decision, records it,
  and walks the router's declared fallback chain.

If a change makes a router touch a provider, or an adapter make a routing choice,
it is the wrong change.

## Conventions

- Providers are reached through local CLIs with existing subscription logins.
  **Do not add code that reads credential files or the macOS keychain.**
- New models go in `turnout.toml`, not in Python.
- Every routing decision must be explainable: a `Candidate` with an empty
  `reasons` list is a bug, because transparency is the product.
- Cost is reported honestly per provider: Claude gives real USD, Copilot gives
  AI credits, Codex gives neither. Never invent a dollar figure.
- Comments explain why, not what. Match the density of the existing modules.

## Commands

    .venv/bin/python -m turnout.cli init       # write a starting turnout.toml
    .venv/bin/python -m turnout.cli check      # probe every provider
    .venv/bin/python -m turnout.cli serve      # http://127.0.0.1:8700
    .venv/bin/python -m turnout.cli export     # dataset from the DB
    .venv/bin/python -m pytest tests/ -q                  # 62 tests, 6 skipped, no network
    ./scripts/start.sh                                    # switchyard + turnout
    ./scripts/smoke.sh                                    # LIVE: costs money

Tests run entirely against a stub adapter. Keep it that way -- a test suite that
needs a logged-in CLI and spends money is a test suite nobody runs.
