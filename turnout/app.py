"""HTTP surface.

Two front doors, deliberately:

  /api/*   Turnout's own API. Rich: it exposes the routing decision, the
           candidate scores, and per-execution telemetry, because making routing
           visible is the point of the project.

  /v1/*    an OpenAI-compatible endpoint. This lets any OpenAI client treat the
           Turnout as a model provider -- including GitHub Copilot CLI in BYOK
           mode (COPILOT_PROVIDER_BASE_URL), which is how Turnout replaces
           Copilot's own opaque `auto` model picker with a router you control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import TurnoutConfig, load_config
from .db import Database
from .domain import (
    ChunkKind,
    Constraints,
    Decision,
    Message,
    Priority,
    RoutingContext,
    RoutingRequest,
    new_id,
)
from .executor import Executor
from .registry import build_adapters, build_routers

log = logging.getLogger("turnout")

STATIC_DIR = Path(__file__).parent / "static"


class Turnout:
    """Everything the request handlers need, assembled once at startup."""

    def __init__(self, cfg: TurnoutConfig):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.catalog = cfg.targets
        self.adapters = build_adapters(cfg)
        self.routers = build_routers(cfg)
        self.executor = Executor(self.adapters, self.catalog, self.db)
        self.active_router = (
            cfg.default_router if cfg.default_router in self.routers else "heuristic"
        )
        self.probe_results: dict[str, tuple[bool, str]] = {}

    async def refresh_probes(self) -> None:
        self.probe_results = await self.executor.probe_all()

    def context(self, session_id: str) -> RoutingContext:
        return RoutingContext(
            catalog=self.catalog,
            session_history=self.db.session_target_history(session_id),
            target_stats=self.db.target_stats(),
        )

    async def decide(self, req: RoutingRequest, router_name: str | None = None) -> Decision:
        """Route, honouring a human pin above any router, and degrading to the
        heuristic router if the selected one is unreachable."""
        if req.constraints.pin_target:
            pin = req.constraints.pin_target
            target = self.catalog.get(pin)
            if target is None:
                raise HTTPException(400, f"unknown target '{pin}'")
            # A pin outranks a router, but it does not outrank reality or the
            # request's own limits. Silently honouring a pin that the same
            # request excluded -- or one whose provider is not even reachable --
            # produces a decision that cannot be executed and a fallback chain
            # of length zero. Refuse it and say which rule it broke.
            if not target.enabled:
                raise HTTPException(400, f"target '{pin}' is disabled in the config")
            if target.available is False:
                detail = self.probe_results.get(target.adapter, (False, "unavailable"))[1]
                raise HTTPException(400, f"target '{pin}' is not reachable: {detail}")
            if pin not in {t.id for t in self.catalog.eligible(req.constraints)}:
                raise HTTPException(
                    400,
                    f"target '{pin}' is pinned but excluded by this request's own "
                    f"constraints (deny_targets / allow_targets / require_local)",
                )
            from .domain import Candidate
            return Decision(
                decision_id=new_id("dec"), request_id=req.request_id,
                target_id=target.id, router_name="pinned", router_version="1",
                rationale=f"Pinned by the user to `{target.id}`; no router consulted.",
                candidates=[Candidate(target.id, 1.0, ["pinned by user"])],
                eligible_ids=[t.id for t in self.catalog.eligible(req.constraints)],
                confidence=1.0, overridden=True,
            )

        name = router_name or self.active_router
        router = self.routers.get(name)
        if router is None:
            raise HTTPException(400, f"unknown router '{name}'")
        try:
            return await router.decide(req, self.context(req.session_id))
        except Exception as e:  # noqa: BLE001
            if name == "heuristic":
                raise
            log.warning("router %s failed (%s); falling back to heuristic", name, e)
            d = await self.routers["heuristic"].decide(req, self.context(req.session_id))
            d.rationale = f"[{name} unavailable: {e}] " + d.rationale
            return d


turnout_app: Turnout | None = None


def get_turnout() -> Turnout:
    if turnout_app is None:
        raise HTTPException(503, "turnout not initialised")
    return turnout_app


# --------------------------------------------------------------------------
# Request parsing
# --------------------------------------------------------------------------


def parse_messages(raw: list[dict[str, Any]]) -> list[Message]:
    out: list[Message] = []
    for m in raw:
        content = m.get("content", "")
        if isinstance(content, list):
            # OpenAI content-parts form; we only support text parts.
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        out.append(Message(role=m.get("role", "user"), content=str(content)))
    return out


def build_constraints(body: dict[str, Any]) -> Constraints:
    c = body.get("constraints") or {}
    pri = c.get("priority") or body.get("priority") or "balanced"
    try:
        priority = Priority(pri)
    except ValueError:
        priority = Priority.BALANCED
    return Constraints(
        priority=priority,
        allow_targets=c.get("allow_targets"),
        deny_targets=c.get("deny_targets") or [],
        max_cost_usd=c.get("max_cost_usd"),
        require_local=bool(c.get("require_local", False)),
        pin_target=c.get("pin_target") or body.get("pin_target"),
    )


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    h = get_turnout()
    await h.refresh_probes()
    ok = [k for k, (v, _) in h.probe_results.items() if v]
    log.info("adapters available: %s", ", ".join(ok) or "none")
    yield
    h.db.close()


def create_app(cfg: TurnoutConfig) -> FastAPI:
    global turnout_app
    turnout_app = Turnout(cfg)
    app = FastAPI(title="Turnout", version="0.1.0", lifespan=lifespan)

    # ---------------------------------------------------------------- native

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        h = get_turnout()
        return {
            "active_router": h.active_router,
            "routers": [
                {"name": n, "version": getattr(r, "version", "?"),
                 "active": n == h.active_router}
                for n, r in h.routers.items()
            ],
            "targets": [
                {**asdict(t), "adapter_detail": h.probe_results.get(t.adapter, (None, ""))[1]}
                for t in h.catalog.targets
            ],
            "adapters": {k: {"ok": v[0], "detail": v[1]} for k, v in h.probe_results.items()},
            "default_target": h.cfg.default_target,
            "switchyard_url": h.cfg.switchyard.url,
        }

    @app.post("/api/router")
    async def set_router(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        h = get_turnout()
        name = body.get("name", "")
        if name not in h.routers:
            raise HTTPException(400, f"unknown router '{name}'")
        h.active_router = name
        return {"active_router": name}

    @app.post("/api/probe")
    async def probe() -> dict[str, Any]:
        h = get_turnout()
        await h.refresh_probes()
        return {k: {"ok": v[0], "detail": v[1]} for k, v in h.probe_results.items()}

    @app.post("/api/route")
    async def route_only(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Decide without executing. The cheapest way to compare routers, and a
        direct mirror of Switchyard's own /v1/decision."""
        h = get_turnout()
        req = RoutingRequest(
            request_id=new_id("req"),
            session_id=body.get("session_id") or new_id("ses"),
            messages=parse_messages(
                body.get("messages") or [{"role": "user", "content": body.get("prompt", "")}]
            ),
            constraints=build_constraints(body),
        )
        names = body.get("routers") or [body.get("router") or h.active_router]
        out = []
        for n in names:
            try:
                d = await h.decide(req, n)
                out.append(asdict(d))
            except HTTPException:
                # A bad pin or unknown router is wrong with the *request*, not
                # with one router, so it must fail the call rather than being
                # filed as one row's error while the response still reads 200.
                raise
            except Exception as e:  # noqa: BLE001 - one router failing must not hide the others
                out.append({"router_name": n, "error": str(e)})
        return {"request_id": req.request_id, "features": req.features(), "decisions": out}

    @app.post("/api/chat")
    async def chat(body: dict[str, Any] = Body(...)) -> StreamingResponse:
        h = get_turnout()
        session_id = body.get("session_id") or new_id("ses")
        req = RoutingRequest(
            request_id=new_id("req"), session_id=session_id,
            messages=parse_messages(body.get("messages") or []),
            constraints=build_constraints(body),
        )
        if not req.messages:
            raise HTTPException(400, "messages is required")
        h.db.record_request(req, client="ui")
        router_name = body.get("router") or h.active_router

        async def gen() -> AsyncIterator[str]:
            try:
                decision = await h.decide(req, router_name)
            except Exception as e:  # noqa: BLE001
                yield sse("error", {"message": str(e)})
                return
            h.db.record_decision(decision)
            yield sse("meta", {
                "request_id": req.request_id, "session_id": session_id,
                "features": req.features(),
            })
            yield sse("decision", asdict(decision))
            try:
                async for chunk in h.executor.run(req, decision):
                    if chunk.kind is ChunkKind.TEXT:
                        yield sse("text", {"text": chunk.text})
                    elif chunk.kind is ChunkKind.REASONING:
                        yield sse("reasoning", {"text": chunk.text})
                    elif chunk.kind is ChunkKind.STATUS:
                        yield sse("status", {"text": chunk.text, **chunk.data})
                    elif chunk.kind is ChunkKind.USAGE:
                        u = chunk.data.get("usage")
                        yield sse("usage", {
                            **{k: v for k, v in chunk.data.items() if k != "usage"},
                            "usage": asdict(u) if u else {},
                        })
                    elif chunk.kind is ChunkKind.ERROR:
                        yield sse("error", {"message": chunk.text, **chunk.data})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                yield sse("error", {"message": f"{type(e).__name__}: {e}"})
            yield sse("done", {"request_id": req.request_id})

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    @app.post("/api/compare")
    async def compare(body: dict[str, Any] = Body(...)) -> StreamingResponse:
        """Run the same prompt on several targets at once.

        This is the counterfactual data the DB needs: logging only the model the
        router picked teaches a future router to imitate this one. Runs are
        marked shadow=1 so they do not pollute the live target statistics.
        """
        h = get_turnout()
        target_ids = body.get("targets") or []
        if len(target_ids) < 2:
            raise HTTPException(400, "compare needs at least 2 targets")
        session_id = body.get("session_id") or new_id("ses")
        messages = parse_messages(body.get("messages") or [])
        req = RoutingRequest(new_id("req"), session_id, messages)
        h.db.record_request(req, client="compare")

        async def run_one(tid: str, queue: asyncio.Queue) -> None:
            from .domain import Candidate
            d = Decision(
                decision_id=new_id("dec"), request_id=req.request_id, target_id=tid,
                router_name="compare", router_version="1",
                rationale=f"Side-by-side comparison run on `{tid}`.",
                candidates=[Candidate(tid, 1.0, ["comparison"])], overridden=True,
            )
            h.db.record_decision(d)
            try:
                async for chunk in h.executor.run(req, d, shadow=True, max_fallbacks=0):
                    if chunk.kind is ChunkKind.TEXT:
                        await queue.put(("text", {"target_id": tid, "text": chunk.text}))
                    elif chunk.kind is ChunkKind.USAGE:
                        u = chunk.data.get("usage")
                        await queue.put(("usage", {
                            "target_id": tid,
                            **{k: v for k, v in chunk.data.items() if k != "usage"},
                            "usage": asdict(u) if u else {},
                        }))
                    elif chunk.kind is ChunkKind.ERROR:
                        await queue.put(("error", {"target_id": tid, "message": chunk.text}))
            finally:
                await queue.put(("target_done", {"target_id": tid}))

        async def gen() -> AsyncIterator[str]:
            queue: asyncio.Queue = asyncio.Queue()
            tasks = [asyncio.create_task(run_one(t, queue)) for t in target_ids]
            yield sse("meta", {"request_id": req.request_id, "session_id": session_id,
                               "targets": target_ids})
            remaining = len(tasks)
            try:
                while remaining:
                    event, data = await queue.get()
                    if event == "target_done":
                        remaining -= 1
                    yield sse(event, data)
            finally:
                for t in tasks:
                    t.cancel()
            yield sse("done", {"request_id": req.request_id})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/feedback")
    async def feedback(body: dict[str, Any] = Body(...)) -> dict[str, str]:
        h = get_turnout()
        fid = new_id("fb")
        h.db.record_feedback(
            fid, body["request_id"], body.get("execution_id"),
            body.get("rating"), body.get("label"), body.get("comment"),
        )
        return {"feedback_id": fid}

    @app.post("/api/preference")
    async def preference(body: dict[str, Any] = Body(...)) -> dict[str, str]:
        h = get_turnout()
        pid = new_id("pref")
        h.db.record_preference(pid, body["request_id"], body["winner"], body["loser"],
                               body.get("judge", "human"))
        return {"preference_id": pid}

    @app.get("/api/history")
    async def history(
        limit: int = Query(60, le=500), session_id: str | None = None,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        h = get_turnout()
        clauses, params = [], []
        if session_id:
            clauses.append("r.session_id = ?")
            params.append(session_id)
        if not include_internal:
            clauses.append("r.client != 'router_internal'")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = h.db.query(
            f"""SELECT r.request_id, r.session_id, r.created_ms, r.prompt, r.client,
                       d.target_id, d.router_name, d.rationale, d.confidence, d.candidates,
                       e.execution_id, e.status, e.latency_ms, e.ttft_ms, e.cost_usd,
                       e.credits, e.output_tokens, e.model_reported, e.response, e.error
                FROM requests r
                LEFT JOIN decisions d  ON d.request_id = r.request_id
                LEFT JOIN executions e ON e.decision_id = d.decision_id AND e.shadow = 0
                {where}
                ORDER BY r.created_ms DESC LIMIT ?""",
            (*params, limit),
        )
        return {"rows": rows}

    @app.get("/api/sessions")
    async def sessions(limit: int = Query(40, le=200)) -> dict[str, Any]:
        h = get_turnout()
        return {"rows": h.db.query(
            """SELECT s.session_id, s.title, s.updated_ms, COUNT(r.request_id) AS n
               FROM sessions s JOIN requests r USING(session_id)
               WHERE r.client != 'router_internal'
               GROUP BY s.session_id HAVING n > 0
               ORDER BY s.updated_ms DESC LIMIT ?""", (limit,))}

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        h = get_turnout()
        return {
            "targets": h.db.query("SELECT * FROM target_stats ORDER BY n DESC"),
            "routers": h.db.query(
                """SELECT d.router_name, COUNT(*) AS n,
                          ROUND(AVG(d.latency_ms),1) AS avg_decision_ms,
                          ROUND(AVG(e.latency_ms),1) AS avg_exec_ms,
                          ROUND(SUM(COALESCE(e.cost_usd,0)),6) AS cost_usd,
                          ROUND(SUM(COALESCE(e.credits,0)),3) AS credits,
                          ROUND(AVG(CASE WHEN e.status='ok' THEN 1.0 ELSE 0.0 END),4) AS success_rate
                   FROM decisions d LEFT JOIN executions e ON e.decision_id=d.decision_id
                   WHERE e.shadow=0 OR e.shadow IS NULL
                   GROUP BY d.router_name ORDER BY n DESC"""),
            "routing_mix": h.db.query(
                """SELECT d.router_name, d.target_id, COUNT(*) AS n
                   FROM decisions d GROUP BY d.router_name, d.target_id ORDER BY n DESC"""),
            "totals": (h.db.query(
                """SELECT COUNT(*) AS requests,
                          (SELECT COUNT(*) FROM executions WHERE shadow=0) AS executions,
                          (SELECT ROUND(SUM(COALESCE(cost_usd,0)),6) FROM executions) AS cost_usd,
                          (SELECT ROUND(SUM(COALESCE(credits,0)),3) FROM executions) AS credits
                   FROM requests""") or [{}])[0],
        }

    @app.get("/api/request/{request_id}")
    async def request_detail(request_id: str) -> dict[str, Any]:
        h = get_turnout()
        req = h.db.query("SELECT * FROM requests WHERE request_id=?", (request_id,))
        if not req:
            raise HTTPException(404, "unknown request")
        return {
            "request": req[0],
            "decisions": h.db.query(
                "SELECT * FROM decisions WHERE request_id=? ORDER BY created_ms", (request_id,)),
            "executions": h.db.query(
                "SELECT * FROM executions WHERE request_id=? ORDER BY started_ms", (request_id,)),
            "feedback": h.db.query("SELECT * FROM feedback WHERE request_id=?", (request_id,)),
        }

    # ------------------------------------------------------- OpenAI-compatible

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        """Every target is a model, plus a virtual `auto` that invokes the router."""
        h = get_turnout()
        entries = [{"id": "auto", "object": "model", "created": int(time.time()),
                    "owned_by": "turnout"}]
        entries += [
            {"id": t.id, "object": "model", "created": int(time.time()),
             "owned_by": t.adapter}
            for t in h.catalog.targets if t.enabled
        ]
        return {"object": "list", "data": entries}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, body: dict[str, Any] = Body(...)):
        """OpenAI-compatible. `model` may be a target id, or `auto` to route.

        Session affinity comes from the `user` field, which is the only stable
        per-conversation identifier the OpenAI schema offers.
        """
        h = get_turnout()
        model = body.get("model") or "auto"
        messages = parse_messages(body.get("messages") or [])
        if not messages:
            raise HTTPException(400, "messages is required")

        constraints = build_constraints(body)
        if model != "auto":
            if h.catalog.get(model) is None:
                raise HTTPException(404, f"unknown model '{model}'")
            constraints.pin_target = model

        session_id = body.get("user") or new_id("ses")
        req = RoutingRequest(
            request_id=new_id("req"), session_id=session_id, messages=messages,
            constraints=constraints, stream=bool(body.get("stream", False)),
        )
        # A routing-time call the Switchyard router made to reach its own
        # decision is not something the user asked for. It is still real spend,
        # so it is recorded -- but under its own client so the history and the
        # request count stay about actual conversations.
        internal = bool(body.get("turnout_internal"))
        h.db.record_request(req, client="router_internal" if internal else "openai_api")
        decision = await h.decide(req)
        h.db.record_decision(decision)

        created = int(time.time())
        cid = f"chatcmpl-{req.request_id}"

        if not req.stream:
            text_parts, usage, err = [], None, None
            async for chunk in h.executor.run(req, decision):
                if chunk.kind is ChunkKind.TEXT:
                    text_parts.append(chunk.text)
                elif chunk.kind is ChunkKind.USAGE:
                    usage = chunk.data.get("usage")
                elif chunk.kind is ChunkKind.ERROR:
                    err = chunk.text
            if err and not text_parts:
                return JSONResponse(
                    {"error": {"message": err, "type": "upstream_error",
                               "param": None, "code": None}}, status_code=502)
            return {
                "id": cid, "object": "chat.completion", "created": created,
                "model": decision.target_id,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "".join(text_parts)}}],
                "usage": {
                    "prompt_tokens": (usage.input_tokens or 0) if usage else 0,
                    "completion_tokens": (usage.output_tokens or 0) if usage else 0,
                    "total_tokens": ((usage.input_tokens or 0) + (usage.output_tokens or 0)) if usage else 0,
                },
                # Non-standard, and namespaced so strict clients ignore it. This
                # is what makes Turnout's routing visible to plain OpenAI
                # clients that have nowhere else to show it.
                "turnout": {
                    "target_id": decision.target_id, "router": decision.router_name,
                    "rationale": decision.rationale, "confidence": decision.confidence,
                    "provider_model": usage.provider_model if usage else None,
                },
            }

        async def gen() -> AsyncIterator[str]:
            head = {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": decision.target_id}
            yield "data: " + json.dumps({
                **head, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                "turnout": {"target_id": decision.target_id,
                                       "router": decision.router_name,
                                       "rationale": decision.rationale},
            }) + "\n\n"
            usage = None
            async for chunk in h.executor.run(req, decision):
                if chunk.kind is ChunkKind.TEXT:
                    yield "data: " + json.dumps({
                        **head, "choices": [{"index": 0, "delta": {"content": chunk.text},
                                             "finish_reason": None}]}) + "\n\n"
                elif chunk.kind is ChunkKind.USAGE:
                    usage = chunk.data.get("usage")
                elif chunk.kind is ChunkKind.ERROR:
                    yield "data: " + json.dumps({
                        **head, "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                        "error": {"message": chunk.text}}) + "\n\n"
            final = {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            if usage:
                final["usage"] = {
                    "prompt_tokens": usage.input_tokens or 0,
                    "completion_tokens": usage.output_tokens or 0,
                    "total_tokens": (usage.input_tokens or 0) + (usage.output_tokens or 0),
                }
            yield "data: " + json.dumps(final) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/health")
    async def health() -> dict[str, Any]:
        h = get_turnout()
        return {"status": "ok", "router": h.active_router,
                "targets": len([t for t in h.catalog.targets if t.available is not False])}

    # ------------------------------------------------------------------ UI

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            f = STATIC_DIR / "index.html"
            if not f.exists():
                raise HTTPException(404, "UI not built")
            return FileResponse(f)

    return app


def app_from_env() -> FastAPI:
    import os
    return create_app(load_config(os.environ.get("TURNOUT_CONFIG", "turnout.toml")))
