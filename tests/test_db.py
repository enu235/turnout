"""Persistence contracts the learning loop depends on."""

from __future__ import annotations

from turnout.domain import (
    Candidate,
    Constraints,
    Decision,
    ExecResult,
    ExecStatus,
    Message,
    Priority,
    RoutingRequest,
    Usage,
    new_id,
)


def test_request_features_are_persisted_for_training(db):
    req = RoutingRequest(new_id("req"), "s1",
                         [Message("user", "```py\nx=1\n```\nwhy?")],
                         Constraints(priority=Priority.FAST))
    db.record_request(req)
    row = db.query("SELECT * FROM requests")[0]
    import json
    feats = json.loads(row["features"])
    assert feats["has_code_fence"] is True
    assert feats["priority"] == "fast"
    assert json.loads(row["constraints"])["priority"] == "fast"


def test_propensity_is_stored_for_off_policy_learning(db):
    req = RoutingRequest(new_id("req"), "s1", [Message("user", "hi")])
    db.record_request(req)
    d = Decision(new_id("dec"), req.request_id, "cheap", "explore", "1", "random pick",
                 candidates=[Candidate("cheap", 1.0, ["random"])])
    db.record_decision(d, propensity=0.25)
    assert db.query("SELECT propensity FROM decisions")[0]["propensity"] == 0.25


def test_target_stats_view_aggregates_correctly(db):
    req = RoutingRequest(new_id("req"), "s1", [Message("user", "hi")])
    db.record_request(req)
    for status, latency in ((ExecStatus.OK, 100), (ExecStatus.OK, 300), (ExecStatus.ERROR, 50)):
        r = ExecResult(new_id("exe"), req.request_id, "cheap", status,
                       started_ms=0, finished_ms=latency, first_token_ms=10)
        r.usage = Usage(output_tokens=5, cost_usd=0.01)
        db.record_execution(r, adapter="stub", model_requested="ok")
    stats = db.target_stats()["cheap"]
    assert stats["n"] == 3
    assert stats["n_ok"] == 2
    assert abs(stats["success_rate"] - 0.6667) < 0.001
    assert stats["total_cost_usd"] == 0.03


def test_session_history_is_most_recent_first(db):
    for i, target in enumerate(("a", "b", "c")):
        req = RoutingRequest(new_id("req"), "s1", [Message("user", "hi")])
        db.record_request(req)
        r = ExecResult(new_id("exe"), req.request_id, target, ExecStatus.OK,
                       started_ms=i * 1000, finished_ms=i * 1000 + 10)
        db.record_execution(r, adapter="stub", model_requested="ok")
    assert db.session_target_history("s1") == ["c", "b", "a"]


def test_preferences_capture_counterfactual_labels(db):
    req = RoutingRequest(new_id("req"), "s1", [Message("user", "hi")])
    db.record_request(req)
    db.record_preference(new_id("pref"), req.request_id, "exe_win", "exe_lose")
    row = db.query("SELECT * FROM preferences")[0]
    assert row["winner_exec"] == "exe_win" and row["judge"] == "human"
