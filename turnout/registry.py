"""Wiring: turn a TurnoutConfig into live adapters and routers.

Adapters are constructed lazily-tolerantly -- an import failure for one provider
must not prevent Turnout from starting with the others, because the whole
point is to keep working when one provider is unavailable.
"""

from __future__ import annotations

import logging

from .config import TurnoutConfig
from .domain import Adapter

log = logging.getLogger("turnout.registry")


def build_adapters(cfg: TurnoutConfig) -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {}

    for module, cls_name, key in (
        ("claude_cli", "ClaudeCliAdapter", "claude_cli"),
        ("copilot_cli", "CopilotCliAdapter", "copilot_cli"),
        ("codex_cli", "CodexCliAdapter", "codex_cli"),
    ):
        try:
            mod = __import__(f"turnout.adapters.{module}", fromlist=[cls_name])
            adapters[key] = getattr(mod, cls_name)(workdir=cfg.cli_workdir)
        except Exception as e:  # noqa: BLE001
            log.warning("adapter %s unavailable: %s", key, e)

    if cfg.http_providers:
        try:
            from .adapters.openai_http import OpenAiHttpAdapter
        except Exception as e:  # noqa: BLE001
            log.warning("openai_http adapter unavailable: %s", e)
        else:
            for p in cfg.http_providers:
                adapters[p.name] = OpenAiHttpAdapter(
                    base_url=p.base_url, api_key_env=p.api_key_env,
                    extra_headers=p.extra_headers, timeout_s=p.timeout_s,
                )
    return adapters


def build_routers(cfg: TurnoutConfig) -> dict[str, object]:
    from .routers.base import ManualRouter
    from .routers.explore import EpsilonGreedyRouter, RandomRouter
    from .routers.heuristic import HeuristicRouter

    heuristic = HeuristicRouter()
    routers: dict[str, object] = {
        "manual": ManualRouter(cfg.default_target),
        "heuristic": heuristic,
        "random": RandomRouter(),
        # Everyday policy that still produces unbiased data: follows the
        # heuristic 90% of the time and samples uniformly the other 10%.
        "explore": EpsilonGreedyRouter(heuristic, epsilon=0.1),
    }
    if cfg.switchyard.enabled:
        try:
            from .routers.switchyard import SwitchyardRouter
        except Exception as e:  # noqa: BLE001
            log.warning("switchyard router unavailable: %s", e)
        else:
            for router_name, route_id in cfg.switchyard.routes.items():
                routers[router_name] = SwitchyardRouter(
                    base_url=cfg.switchyard.url, route=route_id,
                    fallback_target=cfg.default_target, name=router_name,
                )
    return routers
