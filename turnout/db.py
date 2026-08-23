"""SQLite persistence.

Design notes:
  * WAL mode, one writer connection guarded by a lock. SQLite is single-writer;
    pretending otherwise is the classic way to earn "database is locked".
  * Append-only fact tables. Nothing is ever updated in place except feedback.
  * The schema is shaped for *training a router later*, which means recording
    what the router SAW (features, eligible set, scores) alongside what happened
    (latency, cost, outcome) -- not just prompt/response pairs.
  * `propensity` on decisions is the probability the router had of picking this
    target. Without it, any model trained on this data just learns to imitate
    the current router instead of improving on it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .domain import Decision, ExecResult, RoutingRequest, now_ms

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    title       TEXT,
    created_ms  INTEGER NOT NULL,
    updated_ms  INTEGER NOT NULL
);

-- What was asked, and what the router was able to see when it was asked.
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    created_ms   INTEGER NOT NULL,
    messages     TEXT NOT NULL,   -- JSON [{role,content}]
    prompt       TEXT NOT NULL,   -- last user message, denormalised for search
    features     TEXT NOT NULL,   -- JSON, the router-visible feature vector
    constraints  TEXT NOT NULL,   -- JSON
    client       TEXT NOT NULL DEFAULT 'ui',   -- ui | openai_api | cli
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id, created_ms);

-- One row per routing decision. Multiple rows per request are possible
-- (shadow routing, re-routes after failure).
CREATE TABLE IF NOT EXISTS decisions (
    decision_id         TEXT PRIMARY KEY,
    request_id          TEXT NOT NULL,
    target_id           TEXT NOT NULL,
    router_name         TEXT NOT NULL,
    router_version      TEXT NOT NULL,
    rationale           TEXT NOT NULL DEFAULT '',
    candidates          TEXT NOT NULL DEFAULT '[]',  -- JSON [{target_id,score,reasons}]
    eligible_ids        TEXT NOT NULL DEFAULT '[]',
    fallback_order      TEXT NOT NULL DEFAULT '[]',
    constraints_applied TEXT NOT NULL DEFAULT '[]',
    confidence          REAL,
    propensity          REAL,     -- P(this target | router state); for off-policy learning
    overridden          INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    raw                 TEXT,     -- verbatim external router payload
    created_ms          INTEGER NOT NULL,
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_request ON decisions(request_id);
CREATE INDEX IF NOT EXISTS idx_decisions_router  ON decisions(router_name, created_ms);

-- One row per actual provider call, including failed attempts and fallbacks.
CREATE TABLE IF NOT EXISTS executions (
    execution_id     TEXT PRIMARY KEY,
    request_id       TEXT NOT NULL,
    decision_id      TEXT,
    target_id        TEXT NOT NULL,
    adapter          TEXT NOT NULL,
    model_requested  TEXT NOT NULL,
    model_reported   TEXT,        -- what the provider says it used ('auto' hides this)
    status           TEXT NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1,
    shadow           INTEGER NOT NULL DEFAULT 0,  -- 1 = comparison run, not shown to user
    response         TEXT NOT NULL DEFAULT '',
    reasoning        TEXT NOT NULL DEFAULT '',
    error            TEXT,
    started_ms       INTEGER NOT NULL,
    first_token_ms   INTEGER,
    finished_ms      INTEGER NOT NULL,
    latency_ms       INTEGER NOT NULL DEFAULT 0,
    ttft_ms          INTEGER,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cached_tokens    INTEGER,
    reasoning_tokens INTEGER,
    cost_usd         REAL,
    credits          REAL,
    provider_session TEXT,
    usage_raw        TEXT,
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);
CREATE INDEX IF NOT EXISTS idx_exec_request ON executions(request_id);
CREATE INDEX IF NOT EXISTS idx_exec_target  ON executions(target_id, started_ms);
CREATE INDEX IF NOT EXISTS idx_exec_status  ON executions(status, started_ms);

-- Human/automated judgement. The supervision signal for a future learned router.
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id  TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL,
    execution_id TEXT,
    rating       INTEGER,      -- -1 bad, 0 neutral, +1 good
    label        TEXT,         -- free-form tag, e.g. 'wrong-model', 'too-slow'
    comment      TEXT,
    created_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_request ON feedback(request_id);

-- Pairwise preference between two executions of the SAME request. This is the
-- counterfactual signal that lets a learned router beat, not imitate, this one.
CREATE TABLE IF NOT EXISTS preferences (
    preference_id TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    winner_exec   TEXT NOT NULL,
    loser_exec    TEXT NOT NULL,
    judge         TEXT NOT NULL DEFAULT 'human',
    created_ms    INTEGER NOT NULL
);

-- Rolling per-target measurements the router may condition on.
CREATE VIEW IF NOT EXISTS target_stats AS
SELECT
    target_id,
    COUNT(*)                                              AS n,
    SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)          AS n_ok,
    ROUND(AVG(CASE WHEN status='ok' THEN 1.0 ELSE 0.0 END), 4) AS success_rate,
    ROUND(AVG(latency_ms), 1)                             AS avg_latency_ms,
    ROUND(AVG(ttft_ms), 1)                                AS avg_ttft_ms,
    ROUND(SUM(COALESCE(cost_usd, 0)), 6)                  AS total_cost_usd,
    ROUND(SUM(COALESCE(credits, 0)), 3)                   AS total_credits,
    SUM(COALESCE(output_tokens, 0))                       AS total_output_tokens
FROM executions
WHERE shadow = 0
GROUP BY target_id;
"""


def _j(v: Any) -> str:
    return json.dumps(v, default=str)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # -- writes ------------------------------------------------------------

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def ensure_session(self, session_id: str, title: str | None = None) -> None:
        ts = now_ms()
        self._write(
            "INSERT INTO sessions(session_id,title,created_ms,updated_ms) VALUES(?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET updated_ms=excluded.updated_ms, "
            "title=COALESCE(sessions.title, excluded.title)",
            (session_id, title, ts, ts),
        )

    def record_request(self, req: RoutingRequest, client: str = "ui") -> None:
        self.ensure_session(req.session_id, (req.last_user_text or "New session")[:80])
        self._write(
            "INSERT OR REPLACE INTO requests"
            "(request_id,session_id,created_ms,messages,prompt,features,constraints,client)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                req.request_id, req.session_id, req.created_ms,
                _j([m.to_dict() for m in req.messages]),
                req.last_user_text,
                _j(req.features()),
                _j(asdict(req.constraints)),
                client,
            ),
        )

    def record_decision(self, d: Decision, propensity: float | None = None) -> None:
        # The router is the authority on its own propensity; the explicit
        # argument stays as an override for callers that know better.
        propensity = d.propensity if propensity is None else propensity
        self._write(
            "INSERT OR REPLACE INTO decisions"
            "(decision_id,request_id,target_id,router_name,router_version,rationale,"
            "candidates,eligible_ids,fallback_order,constraints_applied,confidence,"
            "propensity,overridden,latency_ms,raw,created_ms)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d.decision_id, d.request_id, d.target_id, d.router_name, d.router_version,
                d.rationale, _j([asdict(c) for c in d.candidates]), _j(d.eligible_ids),
                _j(d.fallback_order), _j(d.constraints_applied), d.confidence,
                propensity, int(d.overridden), d.latency_ms,
                _j(d.raw) if d.raw is not None else None, now_ms(),
            ),
        )

    def record_execution(
        self, r: ExecResult, *, adapter: str, model_requested: str,
        decision_id: str | None = None, shadow: bool = False,
    ) -> None:
        u = r.usage
        self._write(
            "INSERT OR REPLACE INTO executions"
            "(execution_id,request_id,decision_id,target_id,adapter,model_requested,"
            "model_reported,status,attempt,shadow,response,reasoning,error,started_ms,"
            "first_token_ms,finished_ms,latency_ms,ttft_ms,input_tokens,output_tokens,"
            "cached_tokens,reasoning_tokens,cost_usd,credits,provider_session,usage_raw)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.execution_id, r.request_id, decision_id, r.target_id, adapter,
                model_requested, u.provider_model, str(r.status), r.attempt, int(shadow),
                r.text, r.reasoning, r.error, r.started_ms, r.first_token_ms,
                r.finished_ms, r.total_ms, r.ttft_ms, u.input_tokens, u.output_tokens,
                u.cached_input_tokens, u.reasoning_tokens, u.cost_usd, u.credits,
                u.provider_session_id, _j(u.raw),
            ),
        )

    def record_feedback(
        self, fid: str, request_id: str, execution_id: str | None,
        rating: int | None, label: str | None, comment: str | None,
    ) -> None:
        self._write(
            "INSERT OR REPLACE INTO feedback"
            "(feedback_id,request_id,execution_id,rating,label,comment,created_ms)"
            " VALUES(?,?,?,?,?,?,?)",
            (fid, request_id, execution_id, rating, label, comment, now_ms()),
        )

    def record_preference(self, pid: str, request_id: str, winner: str, loser: str,
                          judge: str = "human") -> None:
        self._write(
            "INSERT OR REPLACE INTO preferences"
            "(preference_id,request_id,winner_exec,loser_exec,judge,created_ms)"
            " VALUES(?,?,?,?,?,?)",
            (pid, request_id, winner, loser, judge, now_ms()),
        )

    # -- reads -------------------------------------------------------------

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def target_stats(self) -> dict[str, dict[str, float]]:
        return {r["target_id"]: r for r in self.query("SELECT * FROM target_stats")}

    def session_target_history(self, session_id: str, limit: int = 20) -> list[str]:
        rows = self.query(
            "SELECT e.target_id FROM executions e JOIN requests r USING(request_id)"
            " WHERE r.session_id=? AND e.shadow=0 ORDER BY e.started_ms DESC LIMIT ?",
            (session_id, limit),
        )
        return [r["target_id"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
