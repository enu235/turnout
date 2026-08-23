"""Configuration loading.

Everything Turnout knows about the outside world -- which providers exist,
which models they expose, how expensive each is thought to be -- lives in one
TOML file. Nothing about providers is hard-coded in Python, so adding a model is
an edit, not a code change.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import Target, TargetCatalog


@dataclass
class HttpProvider:
    """One OpenAI-compatible HTTP endpoint, registered as its own adapter name."""

    name: str
    base_url: str
    api_key_env: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 300.0


@dataclass
class SwitchyardConfig:
    enabled: bool = True
    url: str = "http://127.0.0.1:4000"
    # Which generated route each registered Switchyard router asks for, as
    # {router_name: route_id}. The classifier route is the interesting one but
    # it makes a real model call per new session; the others are instant.
    routes: dict[str, str] = field(default_factory=lambda: {
        "switchyard": "switchyard/classifier",
        "switchyard-random": "switchyard/random",
    })
    config_path: str = "config/switchyard.generated.toml"
    # A bare name is looked up on $PATH; anything with a separator resolves
    # against the config file. Switchyard is an optional dependency, so the
    # default must not assume a particular checkout layout.
    binary: str = "switchyard-server"
    port: int = 4000
    routing_log: str = "data/switchyard-routing.jsonl"
    autostart: bool = True


@dataclass
class TurnoutConfig:
    root: Path
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8700
    default_router: str = "heuristic"
    default_target: str = ""
    cli_workdir: str = "~"
    targets: TargetCatalog = field(default_factory=lambda: TargetCatalog([]))
    http_providers: list[HttpProvider] = field(default_factory=list)
    switchyard: SwitchyardConfig = field(default_factory=SwitchyardConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    def resolve_binary(self, name: str) -> Path | None:
        """Locate an executable named in the config.

        Bare names go to $PATH so a system-installed Switchyard just works;
        anything containing a separator is treated as a path relative to the
        config file. Returns None when it cannot be found, because Switchyard
        is optional and its absence must not be fatal.
        """
        import shutil

        if "/" in name or "\\" in name:
            candidate = self.resolve(name)
            return candidate if candidate.exists() else None
        found = shutil.which(name)
        return Path(found) if found else None

    def resolve(self, p: str | Path) -> Path:
        """Relative paths in the config resolve against the config file's own
        directory, not the process cwd -- Turnout is routinely launched
        from elsewhere, and a data directory that moves with $PWD is a trap."""
        p = Path(os.path.expanduser(str(p)))
        return p if p.is_absolute() else (self.root / p).resolve()


def load_config(path: str | Path) -> TurnoutConfig:
    path = Path(os.path.expanduser(str(path))).resolve()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    root = path.parent
    h = raw.get("turnout", {})
    cfg = TurnoutConfig(
        root=root,
        db_path=Path(h.get("db_path", "data/turnout.db")),
        host=h.get("host", "127.0.0.1"),
        port=int(h.get("port", 8700)),
        default_router=h.get("default_router", "heuristic"),
        default_target=h.get("default_target", ""),
        cli_workdir=h.get("cli_workdir", "~"),
        raw=raw,
    )
    cfg.db_path = cfg.resolve(cfg.db_path)

    sy = raw.get("switchyard", {})
    cfg.switchyard = SwitchyardConfig(
        enabled=bool(sy.get("enabled", True)),
        url=sy.get("url", "http://127.0.0.1:4000"),
        config_path=sy.get("config_path", "config/switchyard.generated.toml"),
        binary=sy.get("binary", "switchyard-server"),
        port=int(sy.get("port", 4000)),
        routing_log=sy.get("routing_log", "data/switchyard-routing.jsonl"),
        autostart=bool(sy.get("autostart", True)),
    )
    if sy.get("routes"):
        cfg.switchyard.routes = dict(sy["routes"])

    cfg.http_providers = [
        HttpProvider(
            name=p["name"],
            base_url=p["base_url"],
            api_key_env=p.get("api_key_env"),
            extra_headers=p.get("extra_headers", {}),
            timeout_s=float(p.get("timeout_s", 300.0)),
        )
        for p in raw.get("http_providers", [])
    ]

    targets = [
        Target(
            id=t["id"],
            adapter=t["adapter"],
            model=t.get("model", "default"),
            label=t.get("label", t["id"]),
            cost_tier=int(t.get("cost_tier", 3)),
            speed_tier=int(t.get("speed_tier", 3)),
            quality_tier=int(t.get("quality_tier", 3)),
            tags=list(t.get("tags", [])),
            local=bool(t.get("local", False)),
            enabled=bool(t.get("enabled", True)),
            notes=t.get("notes", ""),
        )
        for t in raw.get("targets", [])
    ]
    seen: set[str] = set()
    for t in targets:
        if t.id in seen:
            raise ValueError(f"duplicate target id in config: {t.id}")
        seen.add(t.id)
    cfg.targets = TargetCatalog(targets)

    if not cfg.default_target and targets:
        cfg.default_target = targets[0].id
    return cfg
