#!/usr/bin/env bash
# Bootstrap Turnout on Omarchy (Arch Linux + Hyprland).
#
# This installs base packages, installs/updates Turnout as a uv tool, writes a
# starter turnout.toml in the chosen workspace (if missing), and runs a probe.
set -euo pipefail

WORKSPACE_DIR="${1:-$HOME/turnout}"
TURNOUT_SPEC="${TURNOUT_SPEC:-git+https://github.com/enu235/turnout}"

if ! command -v omarchy >/dev/null 2>&1; then
  echo "omarchy CLI not found. This installer is for Omarchy systems." >&2
  exit 1
fi

echo "==> Installing system prerequisites (python, uv, git)..."
omarchy pkg add python uv git

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not on PATH after package install." >&2
  echo "Open a new shell and run this script again." >&2
  exit 1
fi

if command -v turnout >/dev/null 2>&1; then
  echo "==> Turnout already installed at $(command -v turnout)"
else
  echo "==> Installing Turnout ($TURNOUT_SPEC)..."
  uv tool install "$TURNOUT_SPEC"
fi

if ! command -v turnout >/dev/null 2>&1; then
  echo "turnout command not found after install." >&2
  echo "Add ~/.local/bin to PATH, then run: turnout --help" >&2
  exit 1
fi

mkdir -p "$WORKSPACE_DIR"
cd "$WORKSPACE_DIR"

if [[ -f turnout.toml ]]; then
  echo "==> Reusing existing $WORKSPACE_DIR/turnout.toml"
else
  echo "==> Writing starter config to $WORKSPACE_DIR/turnout.toml"
  turnout init
fi

echo "==> Probing adapters and targets..."
turnout check

echo
echo "Done."
echo "Start the server with:"
echo "  cd $WORKSPACE_DIR && turnout serve"
echo "Then open: http://127.0.0.1:8700"
