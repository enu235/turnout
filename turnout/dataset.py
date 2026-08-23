"""Export the recorded history as a training dataset.

The database is shaped for learning, but a schema is not a dataset. This module
does the join and produces the two files a router-training experiment actually
needs:

  routing.jsonl      one row per decision: the features the router saw, what it
                     chose, the probability it had of choosing that, and how the
                     call turned out.
  preferences.jsonl  pairwise labels -- "for this prompt, execution A beat B".

The propensity column is what makes the first file more than a record of the
incumbent's habits. Weighting each row by 1/propensity turns a biased log into
an approximately unbiased estimate of how any *other* policy would have done --
which is how you can tell whether a new router is better before deploying it.
That correction is only valid where propensity > 0, so rows from a purely
deterministic router are marked and can be filtered out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database

ROUTING_SQL = """
SELECT
    r.request_id, r.session_id, r.created_ms, r.prompt, r.messages,
    r.features, r.constraints, r.client,
    d.decision_id, d.target_id, d.router_name, d.router_version,
    d.candidates, d.eligible_ids, d.confidence, d.propensity,
    d.overridden, d.latency_ms AS decision_latency_ms,
    e.execution_id, e.adapter, e.model_requested, e.model_reported,
    e.status, e.attempt, e.latency_ms AS exec_latency_ms, e.ttft_ms,
    e.input_tokens, e.output_tokens, e.cost_usd, e.credits, e.response,
    (SELECT AVG(rating) FROM feedback f WHERE f.execution_id = e.execution_id) AS rating
FROM requests r
JOIN decisions  d ON d.request_id = r.request_id
LEFT JOIN executions e ON e.decision_id = d.decision_id
WHERE COALESCE(e.shadow, 0) = 0
ORDER BY r.created_ms
"""

PREFERENCE_SQL = """
SELECT
    p.preference_id, p.request_id, p.judge, p.created_ms,
    r.prompt, r.features,
    w.target_id AS winner_target, w.model_reported AS winner_model,
    w.latency_ms AS winner_latency_ms, w.cost_usd AS winner_cost_usd,
    w.response AS winner_response,
    l.target_id AS loser_target,  l.model_reported AS loser_model,
    l.latency_ms AS loser_latency_ms,  l.cost_usd AS loser_cost_usd,
    l.response AS loser_response
FROM preferences p
JOIN requests   r ON r.request_id = p.request_id
JOIN executions w ON w.execution_id = p.winner_exec
JOIN executions l ON l.execution_id = p.loser_exec
ORDER BY p.created_ms
"""

_JSON_COLUMNS = ("features", "constraints", "candidates", "eligible_ids", "messages")


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    for col in _JSON_COLUMNS:
        if isinstance(row.get(col), str):
            try:
                row[col] = json.loads(row[col])
            except json.JSONDecodeError:
                pass
    return row


def routing_rows(db: Database, include_text: bool = False) -> list[dict[str, Any]]:
    rows = [_hydrate(r) for r in db.query(ROUTING_SQL)]
    for r in rows:
        # A deterministic router reports propensity 1.0, which makes importance
        # weighting a no-op and any counterfactual estimate from it unreliable.
        # Flag it here rather than leaving the consumer to infer it.
        p = r.get("propensity")
        r["usable_for_off_policy"] = bool(p is not None and 0.0 < p < 1.0)
        r["reward_latency_s"] = (r["exec_latency_ms"] or 0) / 1000.0
        r["succeeded"] = r.get("status") == "ok"
        if not include_text:
            r.pop("response", None)
            r.pop("messages", None)
    return rows


def preference_rows(db: Database, include_text: bool = False) -> list[dict[str, Any]]:
    rows = [_hydrate(r) for r in db.query(PREFERENCE_SQL)]
    if not include_text:
        for r in rows:
            r.pop("winner_response", None)
            r.pop("loser_response", None)
    return rows


def write_jsonl(rows: list[dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")
    return len(rows)


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"rows": 0}
    by_router: dict[str, int] = {}
    by_target: dict[str, int] = {}
    for r in rows:
        by_router[r["router_name"]] = by_router.get(r["router_name"], 0) + 1
        by_target[r["target_id"]] = by_target.get(r["target_id"], 0) + 1
    ok = sum(1 for r in rows if r["succeeded"])
    usable = sum(1 for r in rows if r["usable_for_off_policy"])
    return {
        "rows": len(rows),
        "succeeded": ok,
        "usable_for_off_policy": usable,
        "distinct_targets": len(by_target),
        "by_router": dict(sorted(by_router.items(), key=lambda kv: -kv[1])),
        "by_target": dict(sorted(by_target.items(), key=lambda kv: -kv[1])),
    }
