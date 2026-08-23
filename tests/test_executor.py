"""Execution, fallback, and persistence."""

from __future__ import annotations

from conftest import StubAdapter

from turnout.domain import (
    Candidate,
    ChunkKind,
    Decision,
    Message,
    RoutingRequest,
    new_id,
)
from turnout.executor import Executor


def make(catalog, db, target_id: str, fallbacks: list[str] | None = None):
    adapter = StubAdapter()
    ex = Executor({"stub": adapter}, catalog, db)
    req = RoutingRequest(new_id("req"), "s1", [Message("user", "hi")])
    db.record_request(req)
    d = Decision(
        decision_id=new_id("dec"), request_id=req.request_id, target_id=target_id,
        router_name="test", router_version="1", rationale="test",
        candidates=[Candidate(target_id, 1.0, ["test"])],
        fallback_order=fallbacks or [],
    )
    db.record_decision(d)
    return ex, req, d, adapter


async def collect(ex, req, d, **kw):
    kinds: list[tuple[str, str]] = []
    async for c in ex.run(req, d, **kw):
        kinds.append((str(c.kind), c.text))
    return kinds


async def test_successful_execution_streams_and_persists(catalog, db):
    ex, req, d, _ = make(catalog, db, "cheap")
    chunks = await collect(ex, req, d)
    text = "".join(t for k, t in chunks if k == "text")
    assert text == "hello from cheap"
    assert any(k == "reasoning" for k, _ in chunks)
    assert any(k == "usage" for k, _ in chunks)

    rows = db.query("SELECT * FROM executions WHERE request_id=?", (req.request_id,))
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["response"] == "hello from cheap"
    assert row["reasoning"] == "thinking..."
    assert row["cost_usd"] == 0.001
    assert row["model_reported"] == "stub/cheap"
    assert row["ttft_ms"] is not None


async def test_failure_falls_back_along_the_router_chain(catalog, db):
    ex, req, d, adapter = make(catalog, db, "broken", fallbacks=["cheap"])
    chunks = await collect(ex, req, d)
    text = "".join(t for k, t in chunks if k == "text")
    assert text == "hello from cheap"
    assert any(k == "status" and "falling back" in t for k, t in chunks)
    assert adapter.calls == ["broken", "cheap"]

    rows = db.query("SELECT target_id,status,attempt FROM executions WHERE request_id=? ORDER BY attempt",
                    (req.request_id,))
    assert [(r["target_id"], r["status"], r["attempt"]) for r in rows] == [
        ("broken", "error", 1), ("cheap", "ok", 2)]


async def test_failed_attempts_are_recorded_not_swallowed(catalog, db):
    """A fallback that hides the original failure makes the data useless."""
    ex, req, d, _ = make(catalog, db, "broken", fallbacks=["cheap"])
    await collect(ex, req, d)
    failed = db.query("SELECT error FROM executions WHERE status='error'")
    assert failed and failed[0]["error"] == "stub failure"


async def test_exhausted_fallback_chain_reports_an_error(catalog, db):
    ex, req, d, _ = make(catalog, db, "broken", fallbacks=[])
    chunks = await collect(ex, req, d)
    assert any(k == "error" for k, _ in chunks)
    assert not any(k == "text" for k, _ in chunks)


async def test_shadow_runs_are_excluded_from_target_stats(catalog, db):
    ex, req, d, _ = make(catalog, db, "cheap")
    await collect(ex, req, d, shadow=True)
    assert db.query("SELECT * FROM target_stats") == []
    assert db.query("SELECT COUNT(*) n FROM executions")[0]["n"] == 1


async def test_unknown_adapter_is_reported_cleanly(catalog, db):
    from turnout.domain import Target
    catalog.targets.append(Target("weird", "no-such-adapter", "ok", "Weird"))
    ex, req, d, _ = make(catalog, db, "weird")
    chunks = await collect(ex, req, d)
    assert any(k == "error" and "no adapter registered" in t for k, t in chunks)


async def test_probe_marks_targets_unavailable(catalog, db):
    ex = Executor({"stub": StubAdapter(available=False)}, catalog, db)
    await ex.probe_all()
    assert all(t.available is False for t in catalog.targets)
    from turnout.domain import Constraints
    assert catalog.eligible(Constraints()) == []


async def test_abandoning_a_stream_still_records_partial_output(catalog, db):
    """A browser that closes mid-response must not lose what was produced, and
    must not leave the execution looking like a success.

    Starlette closes an abandoned StreamingResponse generator, which raises
    GeneratorExit -- not CancelledError -- at the suspended yield. That is not
    an Exception, so it slips past ordinary handlers; the executor persists in
    a finally block precisely for this case.
    """
    ex, req, d, _ = make(catalog, db, "slow-target")

    agen = ex.run(req, d)
    saw_text = False
    async for chunk in agen:
        if chunk.kind is ChunkKind.TEXT:
            saw_text = True
            break          # abandon the stream mid-response
    await agen.aclose()

    assert saw_text
    rows = db.query("SELECT status, response FROM executions WHERE request_id=?", (req.request_id,))
    assert rows, "an abandoned execution must still be recorded"
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["response"] == "hello", "partial output should be preserved, not discarded"
