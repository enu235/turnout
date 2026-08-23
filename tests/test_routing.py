"""Router behaviour. These are the assertions that stop routing silently rotting."""

from __future__ import annotations

import pytest

from turnout.domain import (
    Constraints,
    Message,
    Priority,
    RoutingContext,
    RoutingRequest,
    new_id,
)
from turnout.routers.base import ManualRouter
from turnout.routers.heuristic import HeuristicRouter


def req(prompt: str, **kw) -> RoutingRequest:
    return RoutingRequest(new_id("req"), "s1", [Message("user", prompt)], Constraints(**kw))


async def decide(router, prompt: str, catalog, **kw):
    return await router.decide(req(prompt, **kw), RoutingContext(catalog))


async def test_manual_router_always_returns_default(catalog):
    r = ManualRouter("mid")
    for prompt in ("hi", "Design a distributed consensus protocol"):
        d = await decide(r, prompt, catalog)
        assert d.target_id == "mid"
        assert d.router_name == "manual"


async def test_easy_prompt_goes_cheap(catalog):
    d = await decide(HeuristicRouter(), "hi", catalog)
    assert d.target_id == "cheap"


async def test_hard_prompt_escalates(catalog):
    d = await decide(
        HeuristicRouter(),
        "Design a zero-downtime migration strategy for a 40TB Postgres cluster, "
        "and analyse the trade-offs of each approach.",
        catalog,
    )
    assert d.target_id in ("mid", "strong"), d.rationale


async def test_quality_priority_beats_cost(catalog):
    easy = "summarise this"
    balanced = await decide(HeuristicRouter(), easy, catalog)
    quality = await decide(HeuristicRouter(), easy, catalog, priority=Priority.QUALITY)
    assert balanced.target_id == "cheap"
    assert quality.target_id == "strong"


async def test_disabled_target_is_never_eligible(catalog):
    d = await decide(HeuristicRouter(), "hi", catalog)
    assert "offline" not in d.eligible_ids


async def test_deny_list_is_respected(catalog):
    d = await decide(HeuristicRouter(), "hi", catalog, deny_targets=["cheap"])
    assert d.target_id != "cheap"
    assert "deny_targets" in " ".join(d.constraints_applied)


async def test_allow_list_is_respected(catalog):
    d = await decide(HeuristicRouter(), "hi", catalog, allow_targets=["strong"])
    assert d.target_id == "strong"


async def test_no_eligible_targets_raises(catalog):
    with pytest.raises(RuntimeError):
        await decide(HeuristicRouter(), "hi", catalog, allow_targets=["nonexistent"])


async def test_every_candidate_is_explained(catalog):
    d = await decide(HeuristicRouter(), "Debug this failing test", catalog)
    assert d.candidates
    assert all(c.reasons for c in d.candidates), "a candidate with no reasons is a black box"
    assert d.rationale


async def test_measured_failures_penalise_a_target(catalog):
    """A target that keeps failing should stop being chosen, even if it looks
    ideal on paper -- this is the loop that makes recorded history matter."""
    ctx = RoutingContext(catalog, target_stats={
        "cheap": {"n": 20, "success_rate": 0.1},
    })
    d = await HeuristicRouter().decide(req("hi"), ctx)
    assert d.target_id != "cheap"


async def test_session_stickiness_applies(catalog):
    plain = await decide(HeuristicRouter(), "summarise this", catalog)
    ctx = RoutingContext(catalog, session_history=["mid"])
    sticky = await HeuristicRouter(sticky_bonus=5.0).decide(req("summarise this"), ctx)
    assert plain.target_id == "cheap"
    assert sticky.target_id == "mid"


# -- exploration ------------------------------------------------------------


async def test_random_router_records_uniform_propensity(catalog):
    from turnout.routers.explore import RandomRouter

    d = await decide(RandomRouter(seed=1), "hi", catalog)
    n = len(d.eligible_ids)
    assert d.propensity == pytest.approx(1.0 / n)
    assert d.explored is True


async def test_random_router_actually_spreads_across_targets(catalog):
    from turnout.routers.explore import RandomRouter

    r = RandomRouter(seed=7)
    picks = {(await decide(r, "hi", catalog)).target_id for _ in range(40)}
    assert len(picks) > 1, "a random router that always picks one target is not exploring"


async def test_epsilon_greedy_mostly_follows_the_inner_router(catalog):
    from turnout.routers.explore import EpsilonGreedyRouter

    r = EpsilonGreedyRouter(HeuristicRouter(), epsilon=0.1, seed=3)
    picks = [(await decide(r, "hi", catalog)).target_id for _ in range(100)]
    assert picks.count("cheap") > 70


async def test_epsilon_greedy_propensities_are_recorded(catalog):
    from turnout.routers.explore import EpsilonGreedyRouter

    r = EpsilonGreedyRouter(HeuristicRouter(), epsilon=0.5, seed=5)
    ds = [await decide(r, "hi", catalog) for _ in range(60)]
    explored = [d for d in ds if d.explored]
    greedy = [d for d in ds if not d.explored]
    assert explored and greedy, "epsilon=0.5 should produce both branches"
    assert all(0 < d.propensity < 1 for d in ds)
    # The greedy branch keeps the exploration mass it could also have come from.
    assert all(d.propensity > 0.5 for d in greedy)


async def test_epsilon_greedy_never_overrides_a_human_pin(catalog):
    from turnout.domain import (
        Constraints,
        Message,
        RoutingContext,
        RoutingRequest,
        new_id,
    )
    from turnout.routers.explore import EpsilonGreedyRouter

    r = EpsilonGreedyRouter(HeuristicRouter(), epsilon=1.0, seed=11)
    for _ in range(10):
        req_ = RoutingRequest(new_id("req"), "s1", [Message("user", "hi")],
                              Constraints(allow_targets=["strong"], pin_target="strong"))
        d = await r.decide(req_, RoutingContext(catalog))
        assert d.explored is False


async def test_each_switchyard_route_gets_its_own_router_name():
    """One SwitchyardRouter instance exists per configured route. The name was
    a class attribute, so every route reported "switchyard" and their decisions
    were indistinguishable in the database and merged in the analytics."""
    from turnout.routers.switchyard import SwitchyardRouter

    a = SwitchyardRouter(route="switchyard/classifier", name="switchyard")
    b = SwitchyardRouter(route="switchyard/random", name="switchyard-random")
    assert a.name != b.name
    assert (a.route, b.route) == ("switchyard/classifier", "switchyard/random")
    assert a.base_url == b.base_url == "http://127.0.0.1:4000"
