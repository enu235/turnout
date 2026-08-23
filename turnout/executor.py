"""Execution: take a Decision, run it, record what happened, fall back if needed.

The executor is the only component allowed to touch providers, and the only one
that writes to the executions table. Routers stay pure; adapters stay dumb.

Fallback deliberately follows the *router's* declared `fallback_order` rather
than a policy of the executor's own. If the router had a view about second
choice, honouring it keeps the recorded data interpretable: every execution can
be attributed to a decision the router actually made.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .db import Database
from .domain import (
    Adapter,
    Chunk,
    ChunkKind,
    Decision,
    ExecRequest,
    ExecResult,
    ExecStatus,
    RoutingRequest,
    Target,
    TargetCatalog,
    Usage,
    new_id,
    now_ms,
)


class Executor:
    def __init__(self, adapters: dict[str, Adapter], catalog: TargetCatalog, db: Database):
        self.adapters = adapters
        self.catalog = catalog
        self.db = db

    def _adapter_for(self, target: Target) -> Adapter:
        a = self.adapters.get(target.adapter)
        if a is None:
            raise KeyError(f"no adapter registered named '{target.adapter}'")
        return a

    async def run(
        self, req: RoutingRequest, decision: Decision, *,
        shadow: bool = False, max_fallbacks: int = 2,
    ) -> AsyncIterator[Chunk]:
        """Stream one logical response, transparently retrying down the fallback
        chain. Every attempt -- including the failed ones -- is persisted.

        A fallback is only attempted if nothing has been emitted yet. Once the
        user has seen output, switching models mid-response would produce a
        Frankenstein answer, so we surface the error instead.
        """
        chain = [decision.target_id] + decision.fallback_order[:max_fallbacks]
        emitted_any = False

        for attempt, target_id in enumerate(chain, start=1):
            target = self.catalog.get(target_id)
            if target is None:
                continue
            try:
                adapter = self._adapter_for(target)
            except KeyError as e:
                yield Chunk(ChunkKind.ERROR, str(e))
                return

            result = ExecResult(
                execution_id=new_id("exe"), request_id=req.request_id,
                target_id=target_id, status=ExecStatus.OK,
                started_ms=now_ms(), attempt=attempt,
            )
            if attempt > 1:
                yield Chunk(
                    ChunkKind.STATUS,
                    f"`{chain[attempt - 2]}` failed; falling back to `{target_id}`",
                    {"fallback_to": target_id, "attempt": attempt},
                )

            text_parts: list[str] = []
            reason_parts: list[str] = []
            failed = False

            exec_req = ExecRequest(
                request_id=req.request_id, target=target, messages=req.messages,
                session_id=req.session_id, stream=req.stream,
            )

            # The row is written in `finally` so that it exists however this
            # unwinds. Task cancellation surfaces as CancelledError, but a
            # consumer that simply abandons the stream -- a browser closing
            # mid-response -- closes this generator and raises GeneratorExit at
            # the yield instead, which is not an Exception and would otherwise
            # slip past every handler and lose the record.
            persisted = False
            try:
                async for chunk in adapter.stream(exec_req):
                    if chunk.kind is ChunkKind.TEXT:
                        if result.first_token_ms is None:
                            result.first_token_ms = now_ms()
                        text_parts.append(chunk.text)
                        emitted_any = True
                        yield chunk
                    elif chunk.kind is ChunkKind.REASONING:
                        if result.first_token_ms is None:
                            result.first_token_ms = now_ms()
                        reason_parts.append(chunk.text)
                        yield chunk
                    elif chunk.kind is ChunkKind.STATUS:
                        yield chunk
                    elif chunk.kind is ChunkKind.USAGE:
                        result.usage = chunk.data.get("usage") or Usage()
                    elif chunk.kind is ChunkKind.ERROR:
                        failed = True
                        result.status = (
                            ExecStatus.TIMEOUT if "timed out" in chunk.text.lower()
                            else ExecStatus.ERROR
                        )
                        result.error = chunk.text
            except (asyncio.CancelledError, GeneratorExit):
                result.status = ExecStatus.CANCELLED
                raise
            except Exception as e:  # noqa: BLE001 - an adapter bug must not kill the request
                failed = True
                result.status = ExecStatus.ERROR
                result.error = f"{type(e).__name__}: {e}"
            finally:
                if not persisted:
                    persisted = True
                    result.finished_ms = now_ms()
                    self._persist(result, target, decision, text_parts, reason_parts, shadow)

            if not failed:
                yield Chunk(ChunkKind.USAGE, "", {
                    "usage": result.usage,
                    "execution_id": result.execution_id,
                    "target_id": target_id,
                    "attempt": attempt,
                    "latency_ms": result.total_ms,
                    "ttft_ms": result.ttft_ms,
                })
                return

            if emitted_any or attempt == len(chain):
                yield Chunk(ChunkKind.ERROR, result.error or "execution failed", {
                    "target_id": target_id, "execution_id": result.execution_id,
                })
                return
            # else: loop and try the next target in the router's chain

    def _persist(
        self, result: ExecResult, target: Target, decision: Decision,
        text_parts: list[str], reason_parts: list[str], shadow: bool,
    ) -> None:
        result.text = "".join(text_parts)
        result.reasoning = "".join(reason_parts)
        self.db.record_execution(
            result, adapter=target.adapter, model_requested=target.model,
            decision_id=decision.decision_id, shadow=shadow,
        )

    async def probe_all(self) -> dict[str, tuple[bool, str]]:
        """Check every adapter concurrently and mark targets available/unavailable.

        Called at startup and on demand. A target whose adapter is missing is
        excluded from routing rather than being allowed to fail at request time.
        """
        names = list(self.adapters)
        results = await asyncio.gather(
            *(self.adapters[n].probe() for n in names), return_exceptions=True
        )
        out: dict[str, tuple[bool, str]] = {}
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                out[name] = (False, f"{type(res).__name__}: {res}")
            else:
                out[name] = res
        for t in self.catalog.targets:
            ok, _ = out.get(t.adapter, (False, "adapter not registered"))
            t.available = ok
        return out
