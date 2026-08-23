"""GitHub Copilot CLI adapter.

Uses `copilot -p --output-format json`, which emits one JSON event per line
covering session bookkeeping, streaming deltas, and a terminal `result`. The
event set is large and mostly internal noise (MCP server bring-up, the full
skills catalog, tool-schema plumbing); we key off the small handful of event
types that actually carry model output or usage, and silently drop the rest.

Every non-`-p` run of this CLI defaults to a *coding agent*: it loads the
GitHub MCP server, the entire personal/project skills catalog, and a large
built-in toolset (bash, edit, view, ...), and folds their schemas into the
system prompt. On this account that overhead alone was ~22k prompt tokens for
a one-word reply -- before the model saw a single character of the actual
request. `--available-tools none` is the load-bearing flag here: "none" does
not match any real tool name, so the allowlist resolves to the empty set and
the tool schemas (and their tokens) never get built. `--disable-builtin-mcps`
and `--no-custom-instructions` trim the rest (the connected github-mcp-server
and any AGENTS.md in the working directory). Net effect: prompt overhead drops
to the CLI's fixed ~2k-token system prompt, this adapter never gains file or
shell access, and startup skips MCP handshakes -- all three of which matter
for a routing harness that is supposed to be comparing chat models, not
agents. `--allow-all-tools` stays because the CLI refuses to run in `-p` mode
without it even though the tool list ends up empty.
"""

from __future__ import annotations

from typing import Any

from ..domain import Chunk, ChunkKind, ExecRequest, Usage
from .base import CliAdapter, single_prompt


class CopilotCliAdapter(CliAdapter):
    name = "copilot_cli"
    binary = "copilot"
    probe_args = ["--version"]

    def build_argv(self, req: ExecRequest) -> list[str]:
        argv = [
            self.binary,
            "-p", single_prompt(req.messages),
            "--output-format", "json",
            "--no-color",
            "--allow-all-tools",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--available-tools", "none",
        ]
        if req.target.model and req.target.model != "default":
            argv += ["--model", req.target.model]
        return argv

    def stdin_payload(self, req: ExecRequest) -> str | None:
        # Unlike `claude -p`, Copilot's -p has no stdin-prompt mode: omitting
        # the argument makes its parser swallow the following flags as extra
        # prompt words instead of reading stdin (verified empirically -- it
        # fails with "Invalid command format", not a hang). The prompt has to
        # go in argv, so we lose the ARG_MAX/`ps`-visibility protection stdin
        # would have given us; there is no CLI-level alternative.
        return None

    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Chunk]:
        t = obj.get("type")
        u: Usage = state["usage"]

        if t == "assistant.reasoning_delta":
            text = obj.get("data", {}).get("deltaContent")
            if text:
                return [Chunk(ChunkKind.REASONING, text)]
            return []

        if t == "assistant.message_delta":
            data = obj.get("data", {})
            text = data.get("deltaContent")
            if text:
                # Remember which message ids were actually streamed so the
                # terminal assistant.message (below) can tell a real delta
                # feed apart from a model that skipped streaming entirely.
                state.setdefault("streamed_message_ids", set()).add(data.get("messageId"))
                state["saw_text"] = True
                return [Chunk(ChunkKind.TEXT, text)]
            return []

        if t == "assistant.message":
            data = obj.get("data", {})
            # This is the one non-ephemeral event carrying the model that
            # actually served the request -- the definitive answer when
            # req.target.model is "auto" and Copilot's own router picked.
            if data.get("model"):
                u.provider_model = data["model"]
            content = data.get("content")
            streamed = state.get("streamed_message_ids", set())
            if content and data.get("messageId") not in streamed:
                # No delta carried this message's text (e.g. streaming was
                # off or the CLI collapsed it into one shot) -- emit the full
                # content now so a response is never silently dropped.
                state["saw_text"] = True
                return [Chunk(ChunkKind.TEXT, content)]
            if not content and data.get("refusal"):
                return [Chunk(ChunkKind.ERROR, f"refused: {data['refusal']}")]
            return []

        if t == "model.model_call_success":
            # The terminal `result` event carries only premium-request counts,
            # not tokens -- the real per-call token/credit breakdown only ever
            # appears here, under data.responseUsage / data.copilotUsage.
            # Summed rather than overwritten in case a turn makes more than
            # one model call (retries, or a future non-chat-only mode).
            data = obj.get("data", {})
            usage = data.get("responseUsage") or {}
            u.input_tokens = (u.input_tokens or 0) + usage.get("prompt_tokens", 0)
            u.output_tokens = (u.output_tokens or 0) + usage.get("completion_tokens", 0)
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            if cached is not None:
                u.cached_input_tokens = (u.cached_input_tokens or 0) + cached
            reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            if reasoning is not None:
                u.reasoning_tokens = (u.reasoning_tokens or 0) + reasoning
            nano_aiu = (data.get("copilotUsage") or {}).get("total_nano_aiu")
            if nano_aiu is not None:
                # "AI credits" in the CLI's own terminal summary is exactly
                # total_nano_aiu / 1e9 (cross-checked against the printed
                # `AI Credits N.NN` line in --output-format text).
                u.credits = (u.credits or 0.0) + nano_aiu / 1e9
            return []

        if t == "result":
            u.provider_session_id = obj.get("sessionId")
            u.raw = {
                "premium_requests": (obj.get("usage") or {}).get("premiumRequests"),
                "total_api_duration_ms": (obj.get("usage") or {}).get("totalApiDurationMs"),
                "session_duration_ms": (obj.get("usage") or {}).get("sessionDurationMs"),
            }
            exit_code = obj.get("exitCode")
            if exit_code not in (0, None):
                # Observed model-not-available failures never reach this
                # point (the CLI dies on stderr before any JSON at all), but
                # a `result` with a nonzero exitCode is still the CLI's own
                # word that something went wrong -- don't let saw_text from
                # partial output paper over that the way the base class's
                # generic rc-check would.
                return [Chunk(ChunkKind.ERROR, f"copilot exited {exit_code}", {"exit_code": exit_code})]
            return []

        # Everything else -- session bring-up, MCP/skills bookkeeping, the
        # model.* shadow copies of the assistant.* events, idle markers -- is
        # internal plumbing with no user-visible content or usage data.
        return []
