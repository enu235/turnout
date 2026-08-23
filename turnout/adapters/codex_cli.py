"""OpenAI Codex CLI adapter.

Uses `codex exec --json`, which emits one JSON object per line describing the
lifecycle of a single turn: `thread.started` -> `turn.started` -> zero or more
`item.completed` events -> `turn.completed` (or `turn.failed`). Unlike Claude
Code's `stream-json`, this is **not token-level streaming** -- there are no
`agent_message_delta`/`item.updated` events for text in the versions we tested
(confirmed on codex-cli 0.149.0 against every model this account can reach).
Each `agent_message` item arrives whole, already complete, in one
`item.completed` line. So `parse_line` yields a handful of large TEXT chunks
per turn rather than a token-by-token trickle; callers that time-to-first-byte
will see it land close to the full response.

Reasoning is counted but never shown: every model in this account's catalog
(`~/.codex/models_cache.json`) ships `"default_reasoning_summary": "none"`, so
`reasoning_output_tokens` is always nonzero-but-silent on hard prompts -- no
`item` with `type: "reasoning"` is ever emitted for us to surface. We still
handle that item type defensively (as REASONING) in case a future model or
account flips its default, but on this account it is unreachable.

`codex exec` is a full coding agent, not a chat completion endpoint: even
under `--sandbox read-only` we watched a plain "what's your model name"
prompt shell out via `sed` to read a bundled self-knowledge skill file and
then fire a real web search, purely because a `.system` skill's description
matched the prompt's keywords. `--sandbox read-only` only stops writes; it
does not stop tool use. We disable the `shell_tool` and `skill_search`
features (both "stable" defaults, not user config) to keep this a comparable
chat surface, matching the reasoning behind Claude Code's `--allowed-tools ""`
above -- an adapter that can go shell out and browse the web makes routing
comparisons meaningless and slow.

No adapter here reports real money for Codex: the JSONL usage block has token
counts only (input/cached/output/reasoning), never a dollar figure, and the
model actually used is never named in any event -- so `provider_model` stays
whatever the caller asked for at the Target level, not something this adapter
can confirm back.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain import Chunk, ChunkKind, ExecRequest, Usage
from .base import CliAdapter, single_prompt


def _unwrap_error(raw: Any) -> str:
    """codex nests the real API error as an escaped JSON string inside a
    `message` field (e.g. `{"message": "{\\"error\\":{\\"message\\":\\"...\\"}}"}`).
    Unwrap it so Turnout surfaces the human-readable reason instead of a
    blob of escaped JSON.
    """
    if isinstance(raw, dict):
        raw = raw.get("message")
    if not isinstance(raw, str):
        return "codex reported an error" if raw is None else str(raw)
    try:
        inner = json.loads(raw)
        msg = (inner.get("error") or {}).get("message")
        return msg if msg else raw
    except (json.JSONDecodeError, AttributeError, TypeError):
        return raw


class CodexCliAdapter(CliAdapter):
    name = "codex_cli"
    binary = "codex"
    probe_args = ["--version"]

    def build_argv(self, req: ExecRequest) -> list[str]:
        argv = [
            self.binary,
            "exec",
            "--json",
            # Required outside a trusted git checkout -- our neutral workdir
            # (see CliAdapter.__init__) is never one.
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            # See module docstring: sandboxing writes isn't enough, these
            # also gate whether the model reaches for tools at all.
            "--disable", "shell_tool",
            "--disable", "skill_search",
        ]
        if req.target.model and req.target.model != "default":
            argv += ["--model", req.target.model]
        argv.append("-")  # read the prompt from stdin, per PROMPT arg docs
        return argv

    def stdin_payload(self, req: ExecRequest) -> str | None:
        # Prompts routinely exceed ARG_MAX and would leak into `ps` via argv.
        return single_prompt(req.messages)

    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Chunk]:
        t = obj.get("type")
        u: Usage = state["usage"]

        if t == "thread.started":
            u.provider_session_id = obj.get("thread_id")
            return []

        if t == "item.completed":
            item = obj.get("item") or {}
            kind = item.get("type")

            if kind == "agent_message":
                text = item.get("text") or ""
                if not text:
                    return []
                state["saw_text"] = True
                return [Chunk(ChunkKind.TEXT, text)]

            if kind == "reasoning":
                # Never observed in practice on this account (see module
                # docstring) but cheap to honor if a model ever emits it.
                text = item.get("text") or ""
                return [Chunk(ChunkKind.REASONING, text)] if text else []

            if kind == "error":
                # A per-item warning (e.g. "unknown model, using fallback
                # metadata") that does not by itself fail the turn -- the
                # turn.failed/error events below carry the real verdict.
                return [Chunk(ChunkKind.STATUS, f"codex warning: {item.get('message')}")]

            if kind in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list"):
                # Only reachable if --disable shell_tool/skill_search didn't
                # cover every path (e.g. a user-configured MCP server). Surface
                # it rather than silently eating agent activity mid-stream.
                detail = item.get("command") or item.get("query") or item.get("path") or ""
                return [Chunk(ChunkKind.STATUS, f"codex {kind}: {detail}".rstrip(": "))]

            return []

        if t == "turn.completed":
            usage = obj.get("usage") or {}
            u.input_tokens = usage.get("input_tokens")
            u.output_tokens = usage.get("output_tokens")
            u.cached_input_tokens = usage.get("cached_input_tokens")
            u.reasoning_tokens = usage.get("reasoning_output_tokens")
            # No cost is ever reported (ChatGPT-plan usage, not metered API
            # billing) -- cost_usd stays None rather than a guessed figure.
            u.raw["cache_write_input_tokens"] = usage.get("cache_write_input_tokens")
            return []

        if t in ("turn.failed", "error"):
            msg = _unwrap_error(obj.get("error") if t == "turn.failed" else obj.get("message"))
            # Also feed the base class's own nonzero-exit fallback (see
            # CliAdapter.stream): codex exits nonzero on these in every case
            # we tested, so that fallback fires too and would otherwise show
            # only "Reading additional input from stdin..." instead of the
            # actual reason.
            if not state["stderr"] or state["stderr"][-1] != msg:
                state["stderr"].append(msg)
            return [Chunk(ChunkKind.ERROR, msg)]

        return []
