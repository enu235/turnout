"""Exploration policies.

A router trained on data produced by another router learns to imitate it. The
recorded decisions only ever show what the incumbent chose, so the model has no
evidence about the targets it never tried -- and it inherits every one of the
incumbent's blind spots.

The fix is to sometimes choose at random, and to record the probability with
which each choice was made. That probability (`propensity`) is what lets an
off-policy learning method correct for the bias later. These two routers exist
to produce that data.
"""

from __future__ import annotations

import random

from ..domain import Candidate, Decision, RoutingContext, RoutingRequest, now_ms
from .base import BaseRouter


class RandomRouter(BaseRouter):
    """Uniform over eligible targets. Pure exploration.

    Useless as a day-to-day policy and genuinely valuable as a data source:
    every target gets tried on every kind of prompt, which is exactly the
    coverage a learned router needs and a good router will never produce.
    """

    name = "random"
    version = "1"

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    async def decide(self, req: RoutingRequest, ctx: RoutingContext) -> Decision:
        t0 = now_ms()
        eligible = ctx.catalog.eligible(req.constraints)
        if not eligible:
            raise RuntimeError("no eligible targets for this request")
        chosen = self._rng.choice(eligible)
        p = 1.0 / len(eligible)
        others = [t for t in eligible if t.id != chosen.id]
        self._rng.shuffle(others)
        d = self._decision(
            req, ctx, target_id=chosen.id,
            rationale=(f"Uniform random over {len(eligible)} eligible targets "
                       f"(p={p:.3f}). Exploration, not preference."),
            candidates=[Candidate(chosen.id, 1.0, ["sampled uniformly at random"])]
            + [Candidate(t.id, 0.0, ["not sampled"]) for t in others],
            started_ms=t0, confidence=None, propensity=p,
        )
        d.explored = True
        return d


class EpsilonGreedyRouter(BaseRouter):
    """Follow a real policy most of the time; explore occasionally.

    This is the one to run day to day once there is a policy worth following.
    It costs `epsilon` of your requests to keep the dataset honest, and unlike
    RandomRouter it stays usable while doing it.
    """

    name = "epsilon-greedy"
    version = "1"

    def __init__(self, inner: BaseRouter, epsilon: float = 0.1, seed: int | None = None):
        if not 0.0 <= epsilon <= 1.0:
            raise ValueError("epsilon must be between 0 and 1")
        self.inner = inner
        self.epsilon = epsilon
        self._rng = random.Random(seed)
        self.name = f"eps{epsilon:g}-{inner.name}"
        self.version = inner.version

    async def health(self) -> tuple[bool, str]:
        return await self.inner.health()

    async def decide(self, req: RoutingRequest, ctx: RoutingContext) -> Decision:
        eligible = ctx.catalog.eligible(req.constraints)
        if not eligible:
            raise RuntimeError("no eligible targets for this request")

        # A pinned target is a human instruction, not a policy output; never
        # spend an exploration step overriding one.
        if req.constraints.pin_target or self._rng.random() >= self.epsilon:
            d = await self.inner.decide(req, ctx)
            n = max(1, len(eligible))
            # The greedy branch still carries exploration probability mass:
            # the inner choice could also have been reached by the random one.
            d.propensity = (1.0 - self.epsilon) + self.epsilon / n
            d.router_name = self.name
            d.rationale = f"[greedy, eps={self.epsilon:g}] " + d.rationale
            return d

        t0 = now_ms()
        chosen = self._rng.choice(eligible)
        p = self.epsilon / len(eligible)
        d = self._decision(
            req, ctx, target_id=chosen.id,
            rationale=(f"Exploration step (eps={self.epsilon:g}): sampled `{chosen.id}` at "
                       f"random from {len(eligible)} eligible targets, overriding "
                       f"`{self.inner.name}`."),
            candidates=[Candidate(chosen.id, 1.0, ["exploration sample"])],
            started_ms=t0, confidence=None, propensity=p,
        )
        d.explored = True
        return d
