"""HTTP surface, exercised against the stub adapter.

Covers both front doors: Turnout's own /api/* and the OpenAI-compatible
/v1/* that lets external tools (GitHub Copilot CLI in BYOK mode) treat the
Turnout as a model provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import StubAdapter
from fastapi.testclient import TestClient

from turnout import app as app_module
from turnout.routers.base import ManualRouter
from turnout.routers.heuristic import HeuristicRouter


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(app_module, "build_adapters", lambda c: {"stub": StubAdapter()})
    monkeypatch.setattr(
        app_module, "build_routers",
        lambda c: {"manual": ManualRouter("cheap"), "heuristic": HeuristicRouter()},
    )
    with TestClient(app_module.create_app(cfg)) as c:
        yield c


def sse_events(text: str) -> list[tuple[str, dict]]:
    out = []
    for frame in text.split("\n\n"):
        name, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


def test_state_lists_targets_and_routers(client):
    s = client.get("/api/state").json()
    assert s["active_router"] == "heuristic"
    assert {r["name"] for r in s["routers"]} == {"manual", "heuristic"}
    assert any(t["id"] == "cheap" for t in s["targets"])
    assert s["adapters"]["stub"]["ok"] is True


def test_router_can_be_switched_at_runtime(client):
    assert client.post("/api/router", json={"name": "manual"}).json()["active_router"] == "manual"
    assert client.get("/api/state").json()["active_router"] == "manual"
    assert client.post("/api/router", json={"name": "nope"}).status_code == 400


def test_route_endpoint_decides_without_executing(client, cfg):
    r = client.post("/api/route", json={"prompt": "hi", "routers": ["heuristic", "manual"]}).json()
    assert [d["router_name"] for d in r["decisions"]] == ["heuristic", "manual"]
    assert r["features"]["n_chars"] == 2
    from turnout.db import Database
    assert Database(cfg.db_path).query("SELECT COUNT(*) n FROM executions")[0]["n"] == 0


def test_chat_streams_decision_then_text_then_usage(client):
    with client.stream("POST", "/api/chat",
                       json={"messages": [{"role": "user", "content": "hi"}]}) as r:
        events = sse_events("".join(r.iter_text()))
    names = [n for n, _ in events]
    assert names[0] == "meta"
    assert names[1] == "decision"
    assert "text" in names and "usage" in names and names[-1] == "done"
    text = "".join(d["text"] for n, d in events if n == "text")
    assert text == "hello from cheap"
    decision = next(d for n, d in events if n == "decision")
    assert decision["candidates"] and all(c["reasons"] for c in decision["candidates"])


def test_pinning_a_target_bypasses_the_router(client):
    with client.stream("POST", "/api/chat",
                       json={"messages": [{"role": "user", "content": "hi"}],
                             "constraints": {"pin_target": "strong"}}) as r:
        events = sse_events("".join(r.iter_text()))
    d = next(v for n, v in events if n == "decision")
    assert d["target_id"] == "strong"
    assert d["overridden"] is True
    assert d["router_name"] == "pinned"


def test_openai_endpoint_non_streaming(client):
    r = client.post("/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": "hi"}]}).json()
    assert r["object"] == "chat.completion"
    assert r["choices"][0]["message"]["content"] == "hello from cheap"
    assert r["usage"]["completion_tokens"] == 3
    # Routing must be visible even to a plain OpenAI client.
    assert r["turnout"]["router"] == "heuristic"


def test_openai_endpoint_streaming_ends_with_done(client):
    with client.stream("POST", "/v1/chat/completions", json={
            "model": "mid", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}) as r:
        body = "".join(r.iter_text())
    assert body.rstrip().endswith("data: [DONE]")
    payloads = [json.loads(line[6:]) for line in body.splitlines()
                if line.startswith("data: ") and line[6:].strip() != "[DONE]"]
    assert "".join(p["choices"][0]["delta"].get("content", "") for p in payloads) == "hello from mid"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_openai_endpoint_rejects_unknown_model(client):
    assert client.post("/v1/chat/completions", json={
        "model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]}).status_code == 404


def test_models_endpoint_advertises_auto_plus_targets(client):
    ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert "auto" in ids and "cheap" in ids
    assert "offline" not in ids  # disabled in config


def test_compare_runs_targets_in_parallel_as_shadow(client, cfg):
    with client.stream("POST", "/api/compare", json={
            "messages": [{"role": "user", "content": "hi"}],
            "targets": ["cheap", "mid"]}) as r:
        events = sse_events("".join(r.iter_text()))
    done = {d["target_id"] for n, d in events if n == "target_done"}
    assert done == {"cheap", "mid"}
    from turnout.db import Database
    rows = Database(cfg.db_path).query("SELECT target_id,shadow FROM executions")
    assert len(rows) == 2 and all(r["shadow"] == 1 for r in rows)


def test_compare_requires_two_targets(client):
    assert client.post("/api/compare", json={
        "messages": [{"role": "user", "content": "hi"}], "targets": ["cheap"]}).status_code == 400


def test_feedback_and_history_round_trip(client):
    with client.stream("POST", "/api/chat",
                       json={"messages": [{"role": "user", "content": "hi"}]}) as r:
        events = sse_events("".join(r.iter_text()))
    rid = next(d for n, d in events if n == "meta")["request_id"]
    eid = next(d for n, d in events if n == "usage")["execution_id"]

    assert client.post("/api/feedback", json={
        "request_id": rid, "execution_id": eid, "rating": 1, "label": "good"}).status_code == 200

    rows = client.get("/api/history").json()["rows"]
    assert rows and rows[0]["request_id"] == rid
    detail = client.get(f"/api/request/{rid}").json()
    assert detail["executions"][0]["execution_id"] == eid
    assert detail["feedback"][0]["rating"] == 1


def test_stats_endpoint_reports_per_target_and_per_router(client):
    client.post("/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    s = client.get("/api/stats").json()
    assert s["totals"]["requests"] >= 1
    assert any(t["target_id"] == "cheap" for t in s["targets"])
    assert any(r["router_name"] == "heuristic" for r in s["routers"])


# -- pin validation ---------------------------------------------------------
#
# A pin outranks the router, but not the request's own limits and not
# reachability. Each of these used to return a decision that could not be
# executed, with an empty fallback chain.


def test_pin_on_a_disabled_target_is_refused(client):
    r = client.post("/api/route", json={
        "prompt": "hi", "constraints": {"pin_target": "offline"}})
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"]


def test_pin_contradicting_deny_list_is_refused(client):
    r = client.post("/api/route", json={
        "prompt": "hi",
        "constraints": {"pin_target": "strong", "deny_targets": ["strong"]}})
    assert r.status_code == 400
    assert "excluded" in r.json()["detail"]


def test_pin_outside_allow_list_is_refused(client):
    r = client.post("/api/route", json={
        "prompt": "hi",
        "constraints": {"pin_target": "strong", "allow_targets": ["cheap"]}})
    assert r.status_code == 400


def test_pin_on_an_unreachable_target_is_refused(client, monkeypatch):
    h = app_module.get_turnout()
    target = h.catalog.get("mid")
    target.available = False
    try:
        r = client.post("/api/route", json={
            "prompt": "hi", "constraints": {"pin_target": "mid"}})
        assert r.status_code == 400
        assert "not reachable" in r.json()["detail"]
    finally:
        target.available = True


def test_a_valid_pin_still_works(client):
    d = client.post("/api/route", json={
        "prompt": "hi", "constraints": {"pin_target": "strong"}}).json()["decisions"][0]
    assert d["target_id"] == "strong"
    assert d["overridden"] is True


def test_declared_cost_cap_is_reported_as_unenforced(client):
    """The cap is not implementable honestly across providers that report
    dollars, credits, and nothing. It must not look like it is in force."""
    d = client.post("/api/route", json={
        "prompt": "hi", "constraints": {"max_cost_usd": 0.01}}).json()["decisions"][0]
    applied = " ".join(d["constraints_applied"])
    assert "max_cost_usd" in applied and "NOT enforced" in applied


def test_router_internal_calls_are_recorded_but_hidden_from_history(client):
    """A routing-time call the Switchyard router makes to reach its own decision
    is real spend, so it is recorded -- but it is not something the user typed,
    so it must not appear in their history or inflate the request count.

    Switchyard stamps these via `extra_body` on every generated target; see
    turnout/switchyard_config.py.
    """
    client.post("/v1/chat/completions", json={
        "model": "cheap", "turnout_internal": True,
        "messages": [{"role": "user", "content": "judge this task"}]})
    client.post("/v1/chat/completions", json={
        "model": "cheap", "messages": [{"role": "user", "content": "a real question"}]})

    default = client.get("/api/history").json()["rows"]
    assert [r["prompt"] for r in default] == ["a real question"]

    everything = client.get("/api/history?include_internal=true").json()["rows"]
    assert {r["prompt"] for r in everything} == {"a real question", "judge this task"}
    internal = next(r for r in everything if r["prompt"] == "judge this task")
    assert internal["client"] == "router_internal"


def test_generated_switchyard_config_tags_every_target_as_internal(cfg):
    """If the tag ever stops being emitted, classifier callbacks silently start
    showing up as user turns again."""
    from turnout import switchyard_config as sc

    toml = sc.generate(cfg)
    n_targets = sum(1 for t in cfg.targets.targets if t.enabled)
    assert toml.count("extra_body = { turnout_internal = true }") == n_targets


def test_packaged_default_config_matches_the_repo_config(tmp_path):
    """`turnout init` ships a copy of the catalog inside the wheel. If the two
    drift, someone who installs the tool gets a different set of models than
    someone who clones the repo."""
    from importlib.resources import files

    from turnout.config import load_config

    packaged = tmp_path / "turnout.toml"
    packaged.write_text(files("turnout").joinpath("default_config.toml").read_text())

    shipped = load_config(packaged)
    repo = load_config(Path(__file__).resolve().parents[1] / "turnout.toml")
    assert [t.id for t in shipped.targets.targets] == [t.id for t in repo.targets.targets]
    assert shipped.default_router == repo.default_router
    assert shipped.default_target == repo.default_target
