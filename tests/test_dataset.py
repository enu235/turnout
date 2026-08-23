"""Dataset export -- the bridge from recorded history to a training set."""

from __future__ import annotations

from turnout import dataset
from turnout.domain import (
    Candidate,
    Decision,
    ExecResult,
    ExecStatus,
    Message,
    RoutingRequest,
    Usage,
    new_id,
)


def _record(db, router: str, target: str, propensity: float | None, status=ExecStatus.OK):
    req = RoutingRequest(new_id("req"), "s1", [Message("user", "hi there")])
    db.record_request(req)
    d = Decision(new_id("dec"), req.request_id, target, router, "1", "because",
                 candidates=[Candidate(target, 1.0, ["r"])], propensity=propensity)
    db.record_decision(d)
    r = ExecResult(new_id("exe"), req.request_id, target, status,
                   started_ms=0, first_token_ms=50, finished_ms=200)
    r.usage = Usage(input_tokens=5, output_tokens=7, cost_usd=0.002)
    r.text = "an answer"
    db.record_execution(r, adapter="stub", model_requested="m", decision_id=d.decision_id)
    return req, d, r


def test_export_joins_features_decision_and_outcome(db):
    _record(db, "heuristic", "cheap", 1.0)
    rows = dataset.routing_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["features"]["n_chars"] == 8       # hydrated from JSON, not a string
    assert row["router_name"] == "heuristic"
    assert row["target_id"] == "cheap"
    assert row["succeeded"] is True
    assert row["reward_latency_s"] == 0.2
    assert row["cost_usd"] == 0.002


def test_deterministic_rows_are_marked_unusable_for_off_policy(db):
    _record(db, "heuristic", "cheap", 1.0)
    _record(db, "explore", "strong", 0.05)
    rows = {r["router_name"]: r for r in dataset.routing_rows(db)}
    assert rows["heuristic"]["usable_for_off_policy"] is False
    assert rows["explore"]["usable_for_off_policy"] is True


def test_text_is_excluded_unless_requested(db):
    _record(db, "heuristic", "cheap", 1.0)
    assert "response" not in dataset.routing_rows(db)[0]
    assert dataset.routing_rows(db, include_text=True)[0]["response"] == "an answer"


def test_failed_executions_are_exported_as_negative_examples(db):
    _record(db, "heuristic", "broken", 1.0, status=ExecStatus.ERROR)
    assert dataset.routing_rows(db)[0]["succeeded"] is False


def test_shadow_runs_are_excluded_from_the_routing_set(db):
    req, d, _ = _record(db, "compare", "cheap", None)
    r = ExecResult(new_id("exe"), req.request_id, "mid", ExecStatus.OK, started_ms=0, finished_ms=10)
    db.record_execution(r, adapter="stub", model_requested="m",
                        decision_id=d.decision_id, shadow=True)
    assert len(dataset.routing_rows(db)) == 1


def test_preferences_export_pairs_both_sides(db):
    req, _, win = _record(db, "compare", "cheap", None)
    lose = ExecResult(new_id("exe"), req.request_id, "mid", ExecStatus.OK,
                      started_ms=0, finished_ms=900)
    lose.text = "a worse answer"
    db.record_execution(lose, adapter="stub", model_requested="m", shadow=True)
    db.record_preference(new_id("pref"), req.request_id, win.execution_id, lose.execution_id)

    rows = dataset.preference_rows(db)
    assert len(rows) == 1
    assert rows[0]["winner_target"] == "cheap"
    assert rows[0]["loser_target"] == "mid"
    assert rows[0]["loser_latency_ms"] == 900


def test_summary_counts_by_router_and_target(db):
    _record(db, "heuristic", "cheap", 1.0)
    _record(db, "heuristic", "mid", 1.0)
    _record(db, "random", "strong", 0.2)
    s = dataset.summarise(dataset.routing_rows(db))
    assert s["rows"] == 3
    assert s["by_router"]["heuristic"] == 2
    assert s["distinct_targets"] == 3
    assert s["usable_for_off_policy"] == 1


def test_summary_of_an_empty_database(db):
    assert dataset.summarise(dataset.routing_rows(db)) == {"rows": 0}
