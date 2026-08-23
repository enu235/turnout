#!/usr/bin/env bash
# Run GitHub Copilot CLI with Turnout as its model provider.
#
# Copilot's BYOK mode accepts any OpenAI-compatible endpoint, so Turnout
# replaces Copilot's own hidden `auto` picker: same CLI, but you choose the
# routing policy and every decision lands in the Turnout database.
#
#   ./scripts/copilot-byok.sh                  # Turnout's router decides
#   ./scripts/copilot-byok.sh claude-opus      # pin one target
#   ./scripts/copilot-byok.sh auto -p "hello"  # pass any copilot flags through
set -euo pipefail
MODEL="${1:-auto}"; shift || true
export COPILOT_PROVIDER_BASE_URL="${TURNOUT_URL:-http://127.0.0.1:8700}/v1"
export COPILOT_PROVIDER_TYPE=openai
export COPILOT_PROVIDER_WIRE_API=completions
export COPILOT_MODEL="$MODEL"
echo "copilot -> $COPILOT_PROVIDER_BASE_URL  (model: $MODEL)" >&2
exec copilot "$@"
