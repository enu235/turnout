# Contributing to Turnout

## The rule that matters

Three concerns are kept deliberately apart, and mixing them is the one change that breaks
this design:

| | Responsibility | May not |
|---|---|---|
| **Router** (`turnout/routers/`) | Decide *which* target serves a request | Touch credentials, spawn processes, retry, or write to the database |
| **Adapter** (`turnout/adapters/`) | Know *how* to reach one provider | Have any opinion about which model should have been chosen |
| **Executor** (`turnout/executor.py`) | Run the decision, record it, walk the router's fallback chain | Invent a fallback the router did not declare |

If a change makes a router touch a provider, or an adapter make a routing choice, it is the
wrong change — that separation is the only reason routers are interchangeable.

## Setting up

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest tests -q          # 62 pass, 6 skipped, no network, no spend
uv run ruff check turnout tests
```

The suite runs entirely against a stub adapter. **Keep it that way.** A test suite that needs
a logged-in CLI and spends real money is a test suite nobody runs, including CI.

There is a second, opt-in suite that does call real providers:

```bash
TURNOUT_LIVE=1 uv run pytest tests/test_live.py -q
```

It costs money. Run it when you change an adapter, not routinely.

## Adding a model

Edit `turnout.toml`. Nothing in Python knows the name of any model, and it should stay that
way. Set the three tiers honestly — they are seed estimates that measured history later
overrides, not marketing:

```toml
[[targets]]
id = "my-model"
adapter = "copilot_cli"      # which adapter runs it
model = "some-model-name"    # the string handed to that adapter
cost_tier = 3                # 1 = cheapest,  5 = most expensive
speed_tier = 2               # 1 = fastest,   5 = slowest
quality_tier = 4             # 1 = weakest,   5 = strongest
tags = ["chat", "code"]
```

Only add a model you have actually called and seen answer. A published model list is not the
same as the models enabled on your account.

## Adding a provider

Subclass `CliAdapter` and implement three methods — `build_argv`, `stdin_payload`, and
`parse_line`. The base class handles the subprocess, line-oriented JSON, stderr, timeouts and
cancellation. `turnout/adapters/claude_cli.py` is the reference implementation; read it first.
For an HTTP provider, add an entry under `[[http_providers]]` instead — no code needed.

See [docs/adapters.md](docs/adapters.md).

## Adding a router

One class implementing `decide(request, context) -> Decision`, registered in
`turnout/registry.py`. See [docs/routing.md](docs/routing.md).

Two hard requirements:

- **Every candidate must carry its reasons.** A `Candidate` with an empty `reasons` list is a
  bug, because transparency is the product. If a router cannot say why, it should not ship.
- **Report an honest propensity.** If your router samples, record the real probability. If it
  is deterministic, report `1.0`. If you genuinely cannot know — as with an external router
  that does not expose one — leave it `None`. A wrong propensity is worse than a missing one:
  it silently corrupts every off-policy estimate computed from the data later.

## House style

- Comments explain **why**, not what. Match the density of the surrounding module.
- Report cost honestly per provider. Claude reports real USD, Copilot reports AI credits,
  Codex reports neither. Never convert between them and never invent a dollar figure.
- Do not add code that reads credential files or the system keychain. Providers are reached
  through CLIs that authenticate themselves.
- Run `ruff` before opening a pull request.

## Reporting a bug

Include the output of `turnout check`, and for anything routing-related the `Decision` from
the inspector panel or from `POST /api/route`. That endpoint returns a decision without
calling a model, so reproductions cost nothing.
