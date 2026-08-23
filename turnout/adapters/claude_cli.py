"""Claude Code CLI adapter.

Uses `claude -p --output-format stream-json`, which emits Anthropic streaming
events wrapped one-per-line. We deliberately run it with an empty MCP config and
no tools: this adapter is a *chat* surface, and an agent that can read files or
run commands would make routing comparisons meaningless (and slow).

Cost comes back as real USD in the terminal `result` event, which makes Claude
the only provider here that reports money rather than credits.
"""

from __future__ import annotations

from typing import Any

from ..domain import Chunk, ChunkKind, ExecRequest, Usage
from .base import CliAdapter, single_prompt


class ClaudeCliAdapter(CliAdapter):
    name = "claude_cli"
    binary = "claude"
    probe_args = ["--version"]

    def build_argv(self, req: ExecRequest) -> list[str]:
        argv = [
            self.binary,
            "-p",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",                 # required for stream-json in -p mode
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--allowed-tools", "",       # chat only: no file or shell access
            "--permission-mode", "default",
        ]
        if req.target.model and req.target.model != "default":
            argv += ["--model", req.target.model]
        return argv

    def stdin_payload(self, req: ExecRequest) -> str | None:
        # Prompt via stdin, never argv: prompts routinely exceed ARG_MAX and
        # argv would leak them into `ps` output.
        return single_prompt(req.messages)

    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Chunk]:
        t = obj.get("type")

        if t == "stream_event":
            ev = obj.get("event", {})
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                dt = delta.get("type")
                if dt == "text_delta" and delta.get("text"):
                    state["saw_text"] = True
                    return [Chunk(ChunkKind.TEXT, delta["text"])]
                if dt == "thinking_delta" and delta.get("thinking"):
                    return [Chunk(ChunkKind.REASONING, delta["thinking"])]
            elif ev.get("type") == "message_start":
                model = ev.get("message", {}).get("model")
                if model:
                    state["usage"].provider_model = model
            return []

        if t == "result":
            u: Usage = state["usage"]
            usage = obj.get("usage", {}) or {}
            u.input_tokens = usage.get("input_tokens")
            u.output_tokens = usage.get("output_tokens")
            u.cached_input_tokens = usage.get("cache_read_input_tokens")
            u.reasoning_tokens = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
            u.cost_usd = obj.get("total_cost_usd")
            u.provider_session_id = obj.get("session_id")
            # `modelUsage` is authoritative about which model actually ran; the
            # message_start model can differ when Claude Code delegates.
            model_usage = obj.get("modelUsage") or {}
            if model_usage and not u.provider_model:
                u.provider_model = next(iter(model_usage), None)
            u.raw = {
                "duration_api_ms": obj.get("duration_api_ms"),
                "num_turns": obj.get("num_turns"),
                "stop_reason": obj.get("stop_reason"),
                "model_usage": model_usage,
            }
            if obj.get("is_error"):
                return [Chunk(ChunkKind.ERROR, str(obj.get("result") or "claude reported an error"))]
            return []

        if t == "rate_limit_event":
            info = obj.get("rate_limit_info", {})
            if info.get("status") not in (None, "allowed"):
                return [Chunk(ChunkKind.STATUS, f"rate limit: {info.get('status')}", info)]
            return []

        return []
