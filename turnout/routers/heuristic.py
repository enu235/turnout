"""A transparent, hand-written routing policy.

This is the honest baseline the whole project is measured against. Every rule is
a named signal with a weight, and every signal that fires is reported back to
the UI, so a routing decision is never a black box the way `--model auto` is.

The weights below are deliberately simple and deliberately visible. They are a
starting point to be replaced by numbers learned from the executions table --
not a claim that these are the right values.
"""

from __future__ import annotations

import re

from ..domain import (
    Candidate,
    Decision,
    Priority,
    RoutingContext,
    RoutingRequest,
    Target,
    now_ms,
)
from .base import BaseRouter

# Signals that a prompt is harder than it looks. Kept as explicit patterns so a
# decision can name the exact phrase that pushed it toward a stronger model.
HARD_PATTERNS = [
    (re.compile(r"\b(architect|design|refactor|migrat|trade-?off|strategy)\w*\b", re.I),
     "design/architecture wording"),
    (re.compile(r"\b(prove|derive|optimi[sz]e|complexity|algorithm)\w*\b", re.I), "analytical wording"),
    (re.compile(r"\b(debug|root cause|why (?:does|is|isn'?t)|failing|regression)\b", re.I),
     "debugging wording"),
    (re.compile(r"\b(security|vulnerab|exploit|auth[eo]ri[sz]|crypto)\w*\b", re.I), "security wording"),
    (re.compile(r"\bstep[- ]by[- ]step\b|\bthink\b.{0,12}\bthrough\b", re.I), "explicit reasoning request"),
]

EASY_PATTERNS = [
    (re.compile(r"^\s*(hi|hey|hello|thanks|thank you|ok|okay|yes|no)\b[\s!.?]*$", re.I),
     "greeting or acknowledgement"),
    (re.compile(r"\b(what is|what'?s|define|meaning of|spell|translate)\b", re.I), "lookup-style question"),
    (re.compile(r"\b(rename|rephrase|shorten|summari[sz]e|tidy|format)\b", re.I), "light text edit"),
]

CODE_PATTERNS = [
    (re.compile(r"```"), "fenced code block"),
    (re.compile(r"\b(function|class|def |import |const |async |SELECT |CREATE TABLE)\b"), "code keywords"),
    (re.compile(r"\.(py|ts|tsx|js|rs|go|java|rb|sql|sh|yaml|toml|json)\b", re.I), "file extension"),
    (re.compile(r"\b(stack ?trace|traceback|exception|compile error|panic)\b", re.I), "error output"),
]


class HeuristicRouter(BaseRouter):
    """Scores every eligible target and picks the best, showing its working."""

    name = "heuristic"
    version = "1"

    def __init__(self, *, sticky_bonus: float = 0.6, failure_penalty: float = 2.0):
        # Session stickiness is cheap and matters a lot: swapping models
        # mid-conversation loses the provider's prompt cache and, with these
        # CLI providers, the conversational thread as well.
        self.sticky_bonus = sticky_bonus
        self.failure_penalty = failure_penalty

    # -- prompt analysis ---------------------------------------------------

    def analyse(self, req: RoutingRequest) -> tuple[float, float, list[str]]:
        """Return (difficulty 0-1, code_affinity 0-1, human-readable signals).

        Keyword hits are counted rather than merely detected, but capped per
        pattern so that one verbose prompt cannot run difficulty to the ceiling
        on repetition alone.
        """
        text = req.prompt_text
        signals: list[str] = []
        difficulty = 0.30
        code = 0.0
        hard_hits = 0

        for rx, why in HARD_PATTERNS:
            n = min(2, len(rx.findall(text)))
            if n:
                hard_hits += n
                difficulty += 0.14 * n
                signals.append(f"+hard: {why} (x{n})" if n > 1 else f"+hard: {why}")
        for rx, why in EASY_PATTERNS:
            if rx.search(text):
                difficulty -= 0.18
                signals.append(f"-easy: {why}")
        for rx, why in CODE_PATTERNS:
            if rx.search(text):
                code += 0.3
                signals.append(f"+code: {why}")

        n_chars = len(text)
        if n_chars > 6000:
            difficulty += 0.25
            signals.append(f"+hard: long prompt ({n_chars:,} chars)")
        elif n_chars > 1500:
            difficulty += 0.12
            signals.append(f"+hard: sizeable prompt ({n_chars:,} chars)")
        elif n_chars < 80 and not hard_hits:
            # Only treat brevity as easiness when nothing else suggests depth.
            # "Prove P != NP" is short and not easy.
            difficulty -= 0.15
            signals.append(f"-easy: very short prompt ({n_chars} chars)")

        if sum(1 for m in req.messages if m.role == "user") > 3:
            difficulty += 0.08
            signals.append("+hard: long multi-turn conversation")

        return max(0.0, min(1.0, difficulty)), min(1.0, code), signals

    # -- scoring -----------------------------------------------------------

    def score(
        self, t: Target, req: RoutingRequest, ctx: RoutingContext,
        difficulty: float, code: float,
    ) -> Candidate:
        reasons: list[str] = []
        s = 0.0

        # Capability floor, then cost. Being under-powered for a task is a real
        # failure; being over-powered is only a waste of money. Treating those
        # two as a symmetric distance -- which an earlier version did -- makes
        # the cheapest model win everything, because it is always "close enough".
        required = 1.0 + difficulty * 4.0        # 1..5
        if t.quality_tier < required:
            deficit = required - t.quality_tier
            s -= deficit * 2.5
            reasons.append(f"under the bar: tier {t.quality_tier} vs required {required:.1f}")
        else:
            s += 1.0
            reasons.append(f"meets the bar: tier {t.quality_tier} >= required {required:.1f}")

        pri = req.constraints.priority
        if pri is Priority.CHEAP:
            s += (6 - t.cost_tier) * 1.1
            reasons.append(f"cheap priority: cost tier {t.cost_tier}")
        elif pri is Priority.FAST:
            s += (6 - t.speed_tier) * 1.1
            reasons.append(f"fast priority: speed tier {t.speed_tier}")
        elif pri is Priority.QUALITY:
            s += t.quality_tier * 1.1
            reasons.append(f"quality priority: tier {t.quality_tier}")
        else:
            # Balanced still prefers not to burn a frontier model on a trivial ask.
            s += (6 - t.cost_tier) * 0.5
            reasons.append(f"balanced: prefer cheaper among capable (cost tier {t.cost_tier})")

        if code > 0.3 and "code" in t.tags:
            s += code * 1.5
            reasons.append("tagged for code, and the prompt looks code-related")

        if ctx.session_history and ctx.session_history[0] == t.id:
            s += self.sticky_bonus
            reasons.append("served the previous turn in this session")

        stats = ctx.target_stats.get(t.id) or {}
        n = stats.get("n") or 0
        if n >= 3:
            rate = stats.get("success_rate")
            if rate is not None and rate < 0.8:
                s -= self.failure_penalty * (1.0 - rate)
                reasons.append(f"measured success rate {rate:.0%} over {int(n)} calls")
            ttft = stats.get("avg_ttft_ms")
            if ttft and pri is Priority.FAST:
                s += max(-1.5, 1.5 - ttft / 4000.0)
                reasons.append(f"measured time-to-first-token {ttft:.0f}ms")

        return Candidate(t.id, round(s, 3), reasons)

    async def decide(self, req: RoutingRequest, ctx: RoutingContext) -> Decision:
        t0 = now_ms()
        eligible = ctx.catalog.eligible(req.constraints)
        if not eligible:
            raise RuntimeError("no eligible targets for this request")

        difficulty, code, signals = self.analyse(req)
        candidates = sorted(
            (self.score(t, req, ctx, difficulty, code) for t in eligible),
            key=lambda c: c.score, reverse=True,
        )
        best = candidates[0]

        # Confidence is the normalised margin over the runner-up. A near-tie is
        # exactly the case worth exploring, and the executor can use this.
        margin = best.score - candidates[1].score if len(candidates) > 1 else 2.0
        confidence = round(min(1.0, max(0.05, margin / 2.5)), 3)

        rationale = (
            f"difficulty {difficulty:.2f}, code affinity {code:.2f} -> `{best.target_id}` "
            f"(score {best.score:+.2f}, margin {margin:+.2f}). "
            + ("Signals: " + "; ".join(signals) if signals else "No strong signals; used defaults.")
        )
        return self._decision(
            req, ctx, target_id=best.target_id, rationale=rationale,
            candidates=candidates, started_ms=t0, confidence=confidence, propensity=1.0,
            raw={"difficulty": difficulty, "code_affinity": code, "signals": signals},
        )
