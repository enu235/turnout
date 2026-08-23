"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import TurnoutConfig, load_config


def _load(args) -> tuple[Path, TurnoutConfig]:
    p = Path(args.config).expanduser()
    if not p.exists():
        print(f"config not found: {p}", file=sys.stderr)
        raise SystemExit(2)
    return p, load_config(p)


def cmd_check(args) -> int:
    from .app import Turnout

    cfg_path, cfg = _load(args)
    h = Turnout(cfg)
    results = asyncio.run(h.executor.probe_all())
    print(f"config      {cfg_path}")
    print(f"database    {cfg.db_path}")
    print(f"router      {h.active_router}   (available: {', '.join(h.routers)})")
    print("\nadapters")
    for name, (ok, detail) in sorted(results.items()):
        print(f"  {'OK ' if ok else '-- '} {name:14} {detail}")
    print("\ntargets")
    for t in cfg.targets.targets:
        mark = "OK " if (t.enabled and t.available) else "-- "
        why = "" if t.enabled else "  (disabled in config)"
        tiers = f"q{t.quality_tier} c{t.cost_tier} s{t.speed_tier}"
        print(f"  {mark}{t.id:16} {t.adapter:12} {t.model:18} {tiers}{why}")
    h.db.close()
    return 0


def cmd_init(args) -> int:
    """Write a starting turnout.toml into the current directory.

    Someone who installed the tool rather than cloning the repository has no
    config to point at, and no obvious way to write one. This gives them the
    same catalog the repository ships with.
    """
    from importlib.resources import files

    dest = Path(args.output).expanduser()
    if dest.exists() and not args.force:
        print(f"{dest} already exists. Pass --force to overwrite it.", file=sys.stderr)
        return 1

    template = files("turnout").joinpath("default_config.toml").read_text()
    dest.write_text(template)
    print(f"wrote {dest}\n")
    print("Next:")
    print("  turnout check      # see which provider CLIs are reachable")
    print("  turnout serve      # http://127.0.0.1:8700")
    print("\nEdit the file to add or remove models. Targets whose CLI is not")
    print("installed are reported as unavailable and skipped, not fatal.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    cfg_path, cfg = _load(args)
    os.environ["TURNOUT_CONFIG"] = str(cfg_path.resolve())
    uvicorn.run(
        "turnout.app:app_from_env", factory=True,
        host=args.host or cfg.host, port=args.port or cfg.port,
        reload=args.reload, log_level="info",
    )
    return 0


def cmd_switchyard(args) -> int:
    from . import switchyard_config as sc

    cfg_path, cfg = _load(args)
    if args.sy_command == "write-config":
        out = sc.write(cfg)
        print(f"wrote {out}")
        return 0

    out = sc.write(cfg)
    binary = cfg.resolve_binary(cfg.switchyard.binary)
    if binary is None:
        print(
            f"switchyard-server not found (looked for {cfg.switchyard.binary!r}).\n"
            "\nSwitchyard is optional -- the other four routers work without it.\n"
            "To enable the two Switchyard routers, build it once:\n"
            "\n  git clone https://github.com/NVIDIA-NeMo/Switchyard\n"
            "  cd Switchyard && cargo build --release -p switchyard-server\n"
            "\nthen either put the binary on your PATH, or set the full path in\n"
            "your config:\n"
            "\n  [switchyard]\n"
            '  binary = "/path/to/Switchyard/target/release/switchyard-server"',
            file=sys.stderr)
        return 2
    argv = [str(binary), "--config", str(out), "--host", "127.0.0.1",
            "--port", str(cfg.switchyard.port),
            "--routing-log-file", str(cfg.resolve(cfg.switchyard.routing_log))]
    if args.sy_command == "validate":
        argv = [str(binary), "--config", str(out), "--dry-run"]
    print("$ " + " ".join(argv))
    return subprocess.call(argv)


def cmd_byok(args) -> int:
    """Print the environment that points GitHub Copilot CLI at Turnout.

    Copilot's BYOK mode takes any OpenAI-compatible endpoint, so Turnout can
    stand in for Copilot's own hidden `auto` model picker -- same CLI, but the
    routing is yours and every decision is recorded.
    """
    _, cfg = _load(args)
    url = f"http://{cfg.host}:{cfg.port}/v1"
    env = {
        "COPILOT_PROVIDER_BASE_URL": url,
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_WIRE_API": "completions",
        "COPILOT_MODEL": args.model,
    }
    if args.export:
        for k, v in env.items():
            print(f'export {k}="{v}"')
        return 0
    print("Point GitHub Copilot CLI at Turnout:\n")
    for k, v in env.items():
        print(f"  {k}={v}")
    print("\n  $ eval \"$(turnout byok --export)\" && copilot")
    print("\nUse COPILOT_MODEL=auto to let Turnout's router choose, or name any target id.")
    return 0


def cmd_export(args) -> int:
    """Write the recorded history out as a training dataset."""
    from . import dataset
    from .db import Database

    _, cfg = _load(args)
    out = Path(args.out).expanduser()
    if not out.is_absolute():
        out = cfg.root / out
    db = Database(cfg.db_path)
    try:
        routing = dataset.routing_rows(db, include_text=args.include_text)
        prefs = dataset.preference_rows(db, include_text=args.include_text)
        n1 = dataset.write_jsonl(routing, out / "routing.jsonl")
        n2 = dataset.write_jsonl(prefs, out / "preferences.jsonl")
    finally:
        db.close()

    import json as _json
    print(f"{out}/routing.jsonl      {n1} rows")
    print(f"{out}/preferences.jsonl  {n2} rows")
    print()
    print(_json.dumps(dataset.summarise(routing), indent=2))
    if n1 and not any(r["usable_for_off_policy"] for r in routing):
        print("\nNote: every row came from a deterministic router, so none of it supports\n"
              "off-policy evaluation. Run the `explore` or `random` router, or use\n"
              "/api/compare, to generate data that can tell you whether a new router\n"
              "would have done better.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="turnout",
        description="Turnout -- a local model router that shows its work")
    p.add_argument("--config", default=os.environ.get("TURNOUT_CONFIG", "turnout.toml"))
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="run the web server (default)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve, host=None, port=None, reload=False)

    sub.add_parser("check", help="validate config and probe every adapter").set_defaults(func=cmd_check)

    ini = sub.add_parser("init", help="write a starting turnout.toml into this directory")
    ini.add_argument("--output", default="turnout.toml")
    ini.add_argument("--force", action="store_true", help="overwrite an existing file")
    ini.set_defaults(func=cmd_init)

    sy = sub.add_parser("switchyard", help="manage the Switchyard router process")
    sy.add_argument("sy_command", choices=["write-config", "validate", "serve"])
    sy.set_defaults(func=cmd_switchyard)

    ex = sub.add_parser("export", help="export the recorded history as a training dataset")
    ex.add_argument("--out", default="data/dataset")
    ex.add_argument("--include-text", action="store_true",
                    help="include full prompts and responses (large, and sensitive)")
    ex.set_defaults(func=cmd_export)

    by = sub.add_parser("byok", help="print env to point GitHub Copilot CLI at Turnout")
    by.add_argument("--model", default="auto")
    by.add_argument("--export", action="store_true", help="emit shell export lines")
    by.set_defaults(func=cmd_byok)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                        datefmt="%H:%M:%S")
    if not getattr(args, "func", None):
        args.func, args.host, args.port, args.reload = cmd_serve, None, None, False
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
