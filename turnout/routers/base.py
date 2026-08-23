"""Routers: pure policy. Given a request and what is known about the world,
choose a target and explain why.

A router may not spawn processes, hold credentials, or retry. That restriction
is the whole point -- it is what lets Switchyard, a hand-written heuristic, and
a future learned model be genuinely interchangeable rather than nominally so.
"""

from __future__ import annotations

from ..domain import (
    Candidate,
    Decision,
    RoutingContext,
    RoutingRequest,
    new_id,
    now_ms,
)


class BaseRouter:
    name = "base"
    version = "0"

    async def decide(self, req: RoutingRequest, ctx: RoutingContext) -> Decision:
        raise NotImplementedError

    async def health(self) -> tuple[bool, str]:
        return True, "in-process"

    # -- helpers for subclasses -------------------------------------------

    def _decision(
        self, req: RoutingRequest, ctx: RoutingContext, *,
        target_id: str, rationale: str, candidates: list[Candidate],
        started_ms: int, confidence: float | None = None,
        raw: dict | None = None, propensity: float | None = None,
    ) -> Decision:
        eligible = [t.id for t in ctx.catalog.eligible(req.constraints)]
        applied: list[str] = []
        c = req.constraints
        if c.allow_targets is not None:
            applied.append(f"allow_targets={c.allow_targets}")
        if c.deny_targets:
            applied.append(f"deny_targets={c.deny_targets}")
        if c.require_local:
            applied.append("require_local")
        if c.priority.value != "balanced":
            applied.append(f"priority={c.priority.value}")
        if c.max_cost_usd is not None:
            # Recorded, not enforced. Only Claude reports real dollars, so there
            # is no honest per-request cost estimate to filter on across
            # providers. Surfacing it here beats letting the caller believe a
            # cap is in force when nothing checks it.
            applied.append(f"max_cost_usd={c.max_cost_usd} (declared, NOT enforced)")

        # Fallback order is the remaining candidates by descending score. A
        # router that ranks its own alternatives degrades more sensibly than
        # an executor guessing at replacements.
        fallback = [c_.target_id for c_ in candidates if c_.target_id != target_id]

        return Decision(
            decision_id=new_id("dec"),
            request_id=req.request_id,
            target_id=target_id,
            router_name=self.name,
            router_version=self.version,
            rationale=rationale,
            candidates=candidates,
            fallback_order=fallback,
            confidence=confidence,
            latency_ms=now_ms() - started_ms,
            eligible_ids=eligible,
            constraints_applied=applied,
            raw=raw,
            propensity=propensity,
        )


class ManualRouter(BaseRouter):
    """No intelligence at all: always the configured default.

    This exists to be the control condition. Any smarter router has to beat it
    on the recorded data, and if it cannot, the sophistication is not paying rent.
    """

    name = "manual"
    version = "1"

    def __init__(self, default_target: str):
        self.default_target = default_target

    async def decide(self, req: RoutingRequest, ctx: RoutingContext) -> Decision:
        t0 = now_ms()
        eligible = ctx.catalog.eligible(req.constraints)
        if not eligible:
            raise RuntimeError("no eligible targets for this request")
        chosen = next((t for t in eligible if t.id == self.default_target), eligible[0])
        note = "" if chosen.id == self.default_target else " (default unavailable)"
        return self._decision(
            req, ctx,
            target_id=chosen.id,
            rationale=f"Fixed default target `{chosen.id}`{note}. No routing logic applied.",
            candidates=[Candidate(chosen.id, 1.0, ["configured default"])]
            + [Candidate(t.id, 0.0, ["not the default"]) for t in eligible if t.id != chosen.id],
            started_ms=t0,
            confidence=1.0,
            propensity=1.0,
        )
