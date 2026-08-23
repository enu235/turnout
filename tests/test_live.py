"""Live checks against real providers and a running Switchyard.

Skipped by default: these spend real money and credits and need CLIs that are
logged in. Run them deliberately after touching an adapter or the Switchyard
integration:

    TURNOUT_LIVE=1 .venv/bin/python -m pytest tests/test_live.py -q -s
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TURNOUT_LIVE"),
    reason="set TURNOUT_LIVE=1 to run tests that call real providers",
)

TURNOUT = os.environ.get("TURNOUT_URL", "http://127.0.0.1:8700")
SWITCHYARD = os.environ.get("SWITCHYARD_URL", "http://127.0.0.1:4000")
CHEAP_PROMPT = "Reply with exactly: OK"


def _running(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def turnout():
    if not _running(TURNOUT):
        pytest.skip(f"turnout not running at {TURNOUT}")
    return TURNOUT


@pytest.fixture(scope="module")
def switchyard():
    if not _running(SWITCHYARD):
        pytest.skip(f"switchyard not running at {SWITCHYARD}")
    return SWITCHYARD


def test_every_adapter_probes_successfully(turnout):
    adapters = httpx.post(f"{turnout}/api/probe", timeout=120).json()
    cli = {k: v for k, v in adapters.items() if k.endswith("_cli")}
    assert cli, "no CLI adapters registered"
    broken = {k: v["detail"] for k, v in cli.items() if not v["ok"]}
    assert not broken, f"CLI adapters failing to probe: {broken}"


def test_switchyard_decision_endpoint_answers(switchyard):
    r = httpx.post(f"{switchyard}/v1/decision", timeout=30, json={
        "input_format": "openai_chat",
        "request": {"model": "switchyard/random",
                    "messages": [{"role": "user", "content": "hello"}]},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["selected"]["model"]
    assert isinstance(body["fallbacks"], list)


def test_switchyard_router_decides_without_executing(turnout, switchyard):
    r = httpx.post(f"{turnout}/api/route", timeout=180, json={
        "prompt": "hello", "routers": ["switchyard-random"]}).json()
    d = r["decisions"][0]
    assert "error" not in d, d.get("error")
    assert d["target_id"]
    # Switchyard exposes no score, so an invented confidence would be a lie.
    assert d["confidence"] is None


def test_a_real_call_streams_and_records_cost(turnout):
    with httpx.stream("POST", f"{turnout}/api/chat", timeout=400, json={
            "messages": [{"role": "user", "content": CHEAP_PROMPT}],
            "constraints": {"pin_target": "claude-haiku"}}) as r:
        body = "".join(r.iter_text())
    assert "event: text" in body
    assert "event: usage" in body
    assert "cost_usd" in body


@pytest.mark.parametrize("target", ["copilot-grok", "copilot-gemini"])
def test_copilot_fronts_other_vendors(turnout, target):
    """Grok and Gemini reachable with no xAI or Google key at all."""
    r = httpx.post(f"{turnout}/v1/chat/completions", timeout=400, json={
        "model": target, "messages": [{"role": "user", "content": CHEAP_PROMPT}]})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["choices"][0]["message"]["content"].strip()
    assert body["turnout"]["provider_model"]
