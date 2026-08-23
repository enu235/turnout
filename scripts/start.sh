#!/usr/bin/env bash
# Start Switchyard and Turnout together, and stop both on Ctrl-C.
#
# Switchyard is optional. Without it you lose the two switchyard routers and
# keep the other four, so a missing binary is a warning, never a failure.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[[ -x "$PY" ]] || PY=$(command -v python3)

$PY -m turnout.cli switchyard write-config >/dev/null || {
  echo "could not generate the switchyard config; continuing without it" >&2
}

# Ask the app where the binary is rather than hard-coding a layout: it honours
# $PATH for a bare name and the config file for a path.
SY_BIN=$($PY - <<'EOF'
from turnout.config import load_config
cfg = load_config("turnout.toml")
p = cfg.resolve_binary(cfg.switchyard.binary)
print(p or "")
EOF
)

if [[ -n "$SY_BIN" ]]; then
  mkdir -p data
  "$SY_BIN" --config config/switchyard.generated.toml --host 127.0.0.1 --port 4000 \
            --routing-log-file data/switchyard-routing.jsonl > data/switchyard.log 2>&1 &
  SY_PID=$!
  trap 'kill $SY_PID 2>/dev/null || true' EXIT
  sleep 2
  if kill -0 $SY_PID 2>/dev/null; then
    echo "switchyard router  http://127.0.0.1:4000   (log: data/switchyard.log)"
  else
    echo "warning: switchyard failed to start; see data/switchyard.log" >&2
  fi
else
  echo "note: switchyard-server not found — starting without the switchyard routers." >&2
  echo "      run '$PY -m turnout.cli switchyard serve' for build instructions." >&2
fi

echo "turnout             http://127.0.0.1:8700"
exec $PY -m turnout.cli serve
