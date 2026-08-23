"""Domain contracts for Turnout.

Three interfaces define the whole system, and they are deliberately kept apart:

    Router   -- decides WHICH target should serve a request. Pure. No credentials,
                no subprocesses, no retries. Swappable: Switchyard, a heuristic,
                a learned policy, or a human clicking a button.
    Adapter  -- knows HOW to talk to one provider. Streams chunks. No opinions
                about which model should have been chosen.
    Executor -- glues them together, records everything, handles fallback.

Keeping Router pure is what makes "swap out Switchyard" a one-line config change
rather than a rewrite.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Message:
    role: str  # system | user | assistant
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


class Priority(StrEnum):
    """What the caller cares about most. Routers read this; they may ignore it."""

    BALANCED = "balanced"
    CHEAP = "cheap"
    FAST = "fast"
    QUALITY = "quality"


@dataclass(slots=True)
class Constraints:
    """Hard limits the router MUST respect, and soft preferences it should weigh."""

    priority: Priority = Priority.BALANCED
    allow_targets: list[str] | None = None      # hard allow-list
    deny_targets: list[str] = field(default_factory=list)
    max_cost_usd: float | None = None
    require_local: bool = False                 # never leave the machine
    pin_target: str | None = None               # human override; always wins


@dataclass(slots=True)
class RoutingRequest:
    """Everything a router is allowed to see. Providers are NOT named here."""

    request_id: str
    session_id: str
    messages: list[Message]
    constraints: Constraints = field(default_factory=Constraints)
    stream: bool = True
    created_ms: int = field(default_factory=now_ms)

    # Cheap features computed once, reused by every router and stored for training.
    @property
    def prompt_text(self) -> str:
        return "\n\n".join(m.content for m in self.messages if m.role != "system")

    @property
    def last_user_text(self) -> str:
        for m in reversed(self.messages):
            if m.role == "user":
                return m.content
        return ""

    def features(self) -> dict[str, Any]:
        """Deterministic, router-visible features. Persisted so a learned router
        can later be trained on exactly what its predecessor saw."""
        text = self.prompt_text
        last = self.last_user_text
        return {
            "n_messages": len(self.messages),
            "n_chars": len(text),
            "n_chars_last": len(last),
            "est_tokens": max(1, len(text) // 4),
            "has_code_fence": "```" in text,
            "n_lines": text.count("\n") + 1,
            "n_question_marks": text.count("?"),
            "is_multi_turn": sum(1 for m in self.messages if m.role == "user") > 1,
            "priority": str(self.constraints.priority),
        }


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Target:
    """A concrete, callable (adapter, model) pair with declared characteristics.

    `cost_tier`/`speed_tier`/`quality_tier` are 1-5 human estimates, not measured
    truth. They seed the heuristic router and the generated Switchyard config;
    once the DB has real executions, measured values should supersede them.
    """

    id: str
    adapter: str                 # which Adapter implementation runs this
    model: str                   # model string handed to that adapter
    label: str
    cost_tier: int = 3           # 1 = cheapest
    speed_tier: int = 3          # 1 = fastest
    quality_tier: int = 3        # 5 = best
    tags: list[str] = field(default_factory=list)
    local: bool = False
    enabled: bool = True
    available: bool | None = None   # filled by probe(); None = unprobed
    notes: str = ""


@dataclass(slots=True)
class TargetCatalog:
    targets: list[Target]

    def get(self, target_id: str) -> Target | None:
        return next((t for t in self.targets if t.id == target_id), None)

    def eligible(self, c: Constraints) -> list[Target]:
        """Apply hard constraints. A router may only choose from this set."""
        out = []
        for t in self.targets:
            if not t.enabled or t.available is False:
                continue
            if c.allow_targets is not None and t.id not in c.allow_targets:
                continue
            if t.id in c.deny_targets:
                continue
            if c.require_local and not t.local:
                continue
            out.append(t)
        return out


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Candidate:
    target_id: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Decision:
    """A router's answer. Deliberately verbose: transparency is the product.

    `fallback_order` is what the executor walks if the selection fails, so a
    router owns its own degradation path rather than the executor guessing.
    """

    decision_id: str
    request_id: str
    target_id: str
    router_name: str
    router_version: str
    rationale: str
    candidates: list[Candidate] = field(default_factory=list)
    fallback_order: list[str] = field(default_factory=list)
    confidence: float | None = None
    latency_ms: int = 0
    eligible_ids: list[str] = field(default_factory=list)
    constraints_applied: list[str] = field(default_factory=list)
    # Verbatim payload from an external router (e.g. Switchyard /v1/decision),
    # kept so we can debug the adapter without re-running the request.
    raw: dict[str, Any] | None = None
    # True when a human pinned the target and the router was bypassed.
    overridden: bool = False
    # P(this target was chosen | router state). Deterministic routers report
    # 1.0; an exploring router reports its actual sampling probability. Without
    # this, anything trained on the recorded data learns to imitate the router
    # that produced it rather than to improve on it.
    propensity: float | None = None
    # Set when this decision was a random exploration rather than the policy's
    # own preference, so training can weight or filter those rows.
    explored: bool = False


@dataclass(slots=True)
class RoutingContext:
    """Observed state a router may condition on. Read-only from the router's view."""

    catalog: TargetCatalog
    session_history: list[str] = field(default_factory=list)   # prior target_ids
    target_stats: dict[str, dict[str, float]] = field(default_factory=dict)


@runtime_checkable
class Router(Protocol):
    """The swap point. Everything else in Turnout is router-agnostic."""

    name: str
    version: str

    async def decide(self, req: RoutingRequest, ctx: RoutingContext) -> Decision: ...

    async def health(self) -> tuple[bool, str]:
        """(ok, detail). Used by the UI and by the executor to fall back."""
        ...


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


class ChunkKind(StrEnum):
    TEXT = "text"          # user-visible token(s)
    REASONING = "reasoning"  # thinking, shown separately
    STATUS = "status"      # adapter progress note
    USAGE = "usage"        # terminal usage/cost payload
    ERROR = "error"


@dataclass(slots=True)
class Chunk:
    kind: ChunkKind
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    credits: float | None = None
    provider_model: str | None = None     # what the provider says it actually used
    provider_session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecRequest:
    request_id: str
    target: Target
    messages: list[Message]
    session_id: str
    stream: bool = True
    timeout_s: float = 300.0


@runtime_checkable
class Adapter(Protocol):
    """How to actually reach one provider family."""

    name: str

    async def probe(self) -> tuple[bool, str]:
        """Is this adapter usable right now? Cheap, no model call."""
        ...

    def stream(self, req: ExecRequest) -> AsyncIterator[Chunk]:
        """Yield chunks. MUST yield exactly one terminal USAGE or ERROR chunk."""
        ...


class ExecStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class ExecResult:
    execution_id: str
    request_id: str
    target_id: str
    status: ExecStatus
    text: str = ""
    reasoning: str = ""
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    started_ms: int = 0
    first_token_ms: int | None = None
    finished_ms: int = 0
    attempt: int = 1

    @property
    def total_ms(self) -> int:
        return max(0, self.finished_ms - self.started_ms)

    @property
    def ttft_ms(self) -> int | None:
        return None if self.first_token_ms is None else self.first_token_ms - self.started_ms
