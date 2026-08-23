"""Test fixtures.

Almost everything here runs against a stub adapter rather than a real provider:
the routing logic, the persistence, and the HTTP surface are all testable
without spending money or depending on a CLI being logged in.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from turnout.config import TurnoutConfig
from turnout.domain import (
    Chunk,
    ChunkKind,
    ExecRequest,
    Target,
    TargetCatalog,
    Usage,
)


class StubAdapter:
    """A deterministic fake provider.

    Behaviour is driven by the target's model string so a single stub can play
    every role a test needs: `ok`, `fail`, `slow`, `empty`.
    """

    name = "stub"

    def __init__(self, available: bool = True):
        self.available = available
        self.calls: list[str] = []

    async def probe(self) -> tuple[bool, str]:
        return self.available, "stub adapter"

    async def stream(self, req: ExecRequest) -> AsyncIterator[Chunk]:
        self.calls.append(req.target.id)
        mode = req.target.model
        if mode == "fail":
            yield Chunk(ChunkKind.ERROR, "stub failure")
            return
        if mode == "slow":
            await asyncio.sleep(0.05)
        yield Chunk(ChunkKind.REASONING, "thinking...")
        for word in ("hello", " from ", req.target.id):
            yield Chunk(ChunkKind.TEXT, word)
        yield Chunk(ChunkKind.USAGE, "", {"usage": Usage(
            input_tokens=10, output_tokens=3, cost_usd=0.001,
            provider_model=f"stub/{req.target.id}",
        )})


def make_catalog() -> TargetCatalog:
    return TargetCatalog([
        Target("cheap", "stub", "ok", "Cheap", cost_tier=1, speed_tier=1, quality_tier=2, tags=["chat"]),
        Target("mid", "stub", "ok", "Mid", cost_tier=3, speed_tier=2, quality_tier=4, tags=["code"]),
        Target("strong", "stub", "ok", "Strong", cost_tier=5, speed_tier=4, quality_tier=5,
               tags=["code", "reasoning"]),
        # Deliberately unattractive on paper so it never wins on score alone;
        # failover tests reach it by pinning or by a fallback chain.
        Target("broken", "stub", "fail", "Broken", cost_tier=5, speed_tier=5, quality_tier=1),
        Target("offline", "stub", "ok", "Offline", enabled=False),
        Target("slow-target", "stub", "slow", "Slow"),
    ])


@pytest.fixture
def catalog() -> TargetCatalog:
    return make_catalog()


@pytest.fixture
def cfg(tmp_path) -> TurnoutConfig:
    c = TurnoutConfig(root=tmp_path, db_path=tmp_path / "test.db",
                      default_target="cheap", default_router="heuristic")
    c.targets = make_catalog()
    c.switchyard.enabled = False
    return c


@pytest.fixture
def db(tmp_path):
    from turnout.db import Database
    d = Database(tmp_path / "t.db")
    yield d
    d.close()
