# Security

## What Turnout has access to

Turnout drives provider CLIs (`claude`, `copilot`, `codex`) as local subprocesses under your
existing logins. It does not read credential files, does not touch the system keychain, and
never sees or transmits an API token. Adapters run with tools disabled, so a provider invoked
through Turnout cannot read your files or run commands the way the same CLI can interactively.

Everything it records — full prompts, full responses, costs, timings — is stored in a local
SQLite file (`data/turnout.db` by default) and sent nowhere except to the providers themselves.

## The API has no authentication

Turnout binds `127.0.0.1` and has no auth on any endpoint. That is a reasonable default for a
single-user local tool, but it means **anything that can reach the port can spend your
subscriptions and read every stored prompt**.

Do not set `host = "0.0.0.0"` on a shared or untrusted network without putting authentication
in front of it. If you expose it deliberately, you own that decision.

## Reporting a vulnerability

Open a GitHub security advisory on this repository rather than a public issue.

Vulnerabilities in NVIDIA NeMo Switchyard belong to
[that project](https://github.com/NVIDIA-NeMo/Switchyard), not here.
