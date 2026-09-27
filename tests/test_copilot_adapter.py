"""Copilot adapter edge cases that are hard to catch in live runs."""

from __future__ import annotations

from turnout.adapters.base import CliAdapter
from turnout.adapters.copilot_cli import CopilotCliAdapter


def test_copilot_adapter_strips_byok_routing_env(monkeypatch):
    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "http://127.0.0.1:8700/v1")
    monkeypatch.setenv("COPILOT_PROVIDER_TYPE", "openai")
    monkeypatch.setenv("COPILOT_PROVIDER_WIRE_API", "completions")
    monkeypatch.setenv("COPILOT_MODEL", "auto-copilot")
    monkeypatch.setenv("TURNOUT_SENTINEL", "kept")

    env = CopilotCliAdapter().runtime_env()

    assert "COPILOT_PROVIDER_BASE_URL" not in env
    assert "COPILOT_PROVIDER_TYPE" not in env
    assert "COPILOT_PROVIDER_WIRE_API" not in env
    assert "COPILOT_MODEL" not in env
    assert env["TURNOUT_SENTINEL"] == "kept"


def test_cli_adapter_keeps_env_by_default(monkeypatch):
    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "http://127.0.0.1:8700/v1")
    env = CliAdapter().runtime_env()
    assert env["COPILOT_PROVIDER_BASE_URL"] == "http://127.0.0.1:8700/v1"
