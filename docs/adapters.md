# Adapters: the provider layer

An adapter is the thin translation layer between a provider (Claude Code, GitHub Copilot, OpenAI, etc.) and Turnout's request/response loop. It handles authentication, command construction, event parsing, and error handling — so Turnout's executor never knows whether it is talking to a subprocess or an HTTP endpoint.

## The Adapter Contract

Every adapter implements a duck-typed Protocol:

```python
async def probe() -> tuple[bool, str]:
    """Is this provider reachable right now?
    
    Returns (available, message) where available is a boolean and message is
    a version string, error reason, or endpoint description. Never raises.
    """

async def stream(req: ExecRequest) -> AsyncIterator[Chunk]:
    """Execute a request and stream its response.
    
    Yields zero or more Chunks of kinds TEXT, REASONING, or STATUS, followed
    by exactly one terminal Chunk of kind USAGE (on success) or ERROR (on failure).
    
    The terminal Chunk's `data` dict must contain {"usage": Usage()} for
    USAGE chunks, and {"exit_code": int} for ERROR chunks from subprocess failures.
    """
```

Chunks are the normalized event units:

```python
@dataclass(slots=True)
class Chunk:
    kind: ChunkKind          # TEXT | REASONING | STATUS | USAGE | ERROR
    text: str = ""           # For TEXT/REASONING/STATUS/ERROR: the content
    data: dict[str, Any] = field(default_factory=dict)  # For USAGE: {"usage": Usage(...)}
```

Every stream terminates with exactly one USAGE or ERROR chunk. That terminal chunk carries the final Usage object, which may be partially populated depending on what the provider reports:

```python
@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    credits: float | None = None
    provider_model: str | None = None
    provider_session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # Provider-specific data
```

Turnout records Usage as-is; downstream analysis can handle sparse fields.

## CliAdapter: Subprocess Pipeline

`CliAdapter` is a base class for providers accessed via local CLI tools. It handles the messy subprocess plumbing so subclasses only write three hooks:

1. **`build_argv(req)`** — assemble the command line
2. **`parse_line(obj, state)`** — translate one parsed JSON line into Chunks
3. **`stdin_payload(req)`** (optional) — return a string to write to stdin, or None to put the prompt in argv

The subprocess pipeline looks like this:

```mermaid
graph LR
    Input["ExecRequest<br/>(prompt + model)"]
    BuildArgv["build_argv()<br/>construct command"]
    Subprocess["subprocess.Popen<br/>read stdout/stderr"]
    Lines["read line-delimited JSON"]
    Parse["parse_line(json_obj)<br/>per-adapter logic"]
    Emit["emit Chunks<br/>TEXT/REASONING/STATUS"]
    Terminal["emit terminal<br/>USAGE or ERROR"]
    Executor["Turnout executor"]
    
    Input --> BuildArgv
    BuildArgv --> Subprocess
    Subprocess --> Lines
    Lines --> Parse
    Parse --> Emit
    Emit --> Terminal
    Terminal --> Executor
    
    classDef input fill:#1e3a5f,stroke:#4a9eff,stroke-width:2px,color:#e8f2ff
    classDef build fill:#4a2c5e,stroke:#b57edc,stroke-width:2px,color:#f5e8ff
    classDef stream fill:#1e4d3a,stroke:#4ade80,stroke-width:2px,color:#e8fff2
    classDef output fill:#5e4a1e,stroke:#fbbf24,stroke-width:2px,color:#fff8e8
    class Input input
    class BuildArgv,Subprocess build
    class Lines,Parse,Emit stream
    class Terminal,Executor output
```

Key responsibilities of `CliAdapter.stream()`:

- Spawns the subprocess with a neutral `cwd` (typically `~`) so the CLI doesn't accidentally read `AGENTS.md` or `CLAUDE.md`
- Reads both stdout and stderr concurrently (stderr is drained asynchronously to avoid deadlock)
- Parses each stdout line as JSON; non-JSON lines are logged to `state["nonjson"]` but never surfaced as model output
- If the process times out, kills it and yields an ERROR chunk
- If the process exits nonzero and no text was seen, yields an ERROR from stderr (last 8 lines)
- If the process exits zero or if text was already emitted, yields a terminal USAGE chunk via `finalize(state)`
- Accumulates usage data across multiple parse_line calls in a per-execution `state` dict

## Claude Code CLI Adapter

**Binary:** `claude`  
**Command:** `claude -p --output-format stream-json --include-partial-messages ... < prompt`

### Key flags

- `--output-format stream-json` — emit token-level streaming events (only output format that supports `-p`)
- `--include-partial-messages` — emit intermediate message states as streaming progresses
- `--verbose` — required for stream-json in `-p` mode
- `--strict-mcp-config` — enforce the provided config shape
- `--mcp-config '{"mcpServers":{}}'` — empty MCP configuration (no servers)
- `--allowed-tools ""` — explicitly disable all tools (chat-only, no file or shell access)
- `--permission-mode default` — explicit permission mode, belt-and-suspenders alongside the empty tool allowlist

### Event stream

The CLI emits one JSON line per event. `parse_line` reads these types:

**`stream_event`** (zero or more per turn)

Contains nested Anthropic streaming events. We key off:

- `event.type == "content_block_delta"` with `delta.type == "text_delta"` → `Chunk(ChunkKind.TEXT, text)`
- `event.type == "content_block_delta"` with `delta.type == "thinking_delta"` → `Chunk(ChunkKind.REASONING, text)`
- `event.type == "message_start"` → record `provider_model` from the message

Example stream_event line:

```json
{
  "type": "stream_event",
  "event": {
    "type": "content_block_delta",
    "index": 0,
    "delta": {
      "type": "text_delta",
      "text": "Hello"
    }
  }
}
```

**`result`** (exactly one, terminal)

The final accounting event. Populates Usage:

```json
{
  "type": "result",
  "usage": {
    "input_tokens": 123,
    "output_tokens": 45,
    "cache_read_input_tokens": 0,
    "output_tokens_details": {
      "thinking_tokens": 5000
    }
  },
  "total_cost_usd": 0.00567,
  "session_id": "uuid-...",
  "duration_api_ms": 1234,
  "num_turns": 1,
  "stop_reason": "end_turn",
  "modelUsage": {
    "claude-opus-5": {
      "inputTokens": 123,
      "outputTokens": 45,
      "costUSD": 0.00567
    }
  },
  "is_error": false
}
```

Fields populated in Usage:

- `input_tokens` — from `usage.input_tokens`
- `output_tokens` — from `usage.output_tokens`
- `cached_input_tokens` — from `usage.cache_read_input_tokens`
- `reasoning_tokens` — from `usage.output_tokens_details.thinking_tokens` (if present)
- **`cost_usd`** — from `total_cost_usd` (real USD)
- `provider_session_id` — from `session_id`
- `provider_model` — from the `message_start` event's model, falling back to the first key of `modelUsage` (only the key is used — the value is a per-model stats object) if `message_start` never set one
- `raw` — `{duration_api_ms, num_turns, stop_reason, model_usage}`

**Why stdin for the prompt?** Prompts routinely exceed the OS `ARG_MAX` limit — 1,048,576 bytes (1 MiB) on this macOS host, shared with the process's environment (`sysctl kern.argmax` / `getconf ARG_MAX`) — and argv leaks into `ps` output where it could expose sensitive context.

### Asymmetry note

Claude is the **only provider here** that reports real USD cost. The other CLI adapters report credits (Copilot) or nothing (Codex). This matters for any cost-based routing decision.

## GitHub Copilot CLI Adapter

**Binary:** `copilot`  
**Command:** `copilot -p <prompt> --output-format json ...`

### Key flags

The unmodified Copilot CLI in interactive mode is a *coding agent*: it loads the GitHub MCP server, every personal/project skill, and a large built-in toolset (bash, file editing, etc.), folding their schemas into the system prompt. On a typical account, that alone adds ~22,000 prompt tokens before the model sees the actual request.

These four flags drop it to a comparable chat surface:

- `--available-tools none` — **load-bearing**: "none" matches no real tool name, so the allowlist resolves to empty and tool schemas never compile into the prompt. Reduces overhead from 22k to ~2k tokens.
- `--disable-builtin-mcps` — disables the bundled GitHub MCP server
- `--no-custom-instructions` — ignores `AGENTS.md` in the working directory
- `--allow-all-tools` — required in `-p` mode even though the tool list is empty (CLI implementation detail)

**Why argv for the prompt?** The `-p` flag has no stdin-prompt mode. Omitting the argument makes the parser treat subsequent flags as additional prompt words. The prompt must go in argv; there is no alternative. This means large prompts are visible in `ps` output—a trade-off inherent to this provider's CLI.

### Event stream

Events are line-delimited JSON. Most are internal bookkeeping (MCP server bring-up, skills catalog, tool-schema plumbing). We key off a small set:

**`assistant.message_delta`** (zero or more per turn)

Streaming text delta. Contains `data.messageId` (unique per message) and `data.deltaContent`.

```json
{
  "type": "assistant.message_delta",
  "data": {
    "messageId": "msg-123",
    "deltaContent": "The answer is"
  }
}
```

**`assistant.reasoning_delta`** (zero or more per turn, reasoning models only)

Streaming reasoning delta.

```json
{
  "type": "assistant.reasoning_delta",
  "data": {
    "deltaContent": "Let me think about this..."
  }
}
```

**`assistant.message`** (one per turn, terminal text event)

The complete, final message. Also the authoritative source for `provider_model` when routing is on "auto".

```json
{
  "type": "assistant.message",
  "data": {
    "messageId": "msg-123",
    "content": "The complete response",
    "model": "claude-sonnet-5",
    "refusal": null
  }
}
```

If a message has no deltas (streaming was off or the CLI skipped it), its full content arrives here. If it was streamed, `content` is empty/null. A refusal field signals a refused request.

**`model.model_call_success`** (zero or more per turn)

Token and credit breakdown. Token counts are here; the terminal `result` event carries only premium-request counts. Can occur multiple times per turn (retries, future non-chat modes).

```json
{
  "type": "model.model_call_success",
  "data": {
    "responseUsage": {
      "prompt_tokens": 234,
      "completion_tokens": 45,
      "prompt_tokens_details": {
        "cached_tokens": 100
      },
      "completion_tokens_details": {
        "reasoning_tokens": 2000
      }
    },
    "copilotUsage": {
      "total_nano_aiu": 920000000
    }
  }
}
```

Fields summed into Usage:

- `input_tokens` — from `responseUsage.prompt_tokens` (summed across events)
- `output_tokens` — from `responseUsage.completion_tokens` (summed)
- `cached_input_tokens` — from `responseUsage.prompt_tokens_details.cached_tokens` (summed)
- `reasoning_tokens` — from `responseUsage.completion_tokens_details.reasoning_tokens` (summed)
- **`credits`** — from `copilotUsage.total_nano_aiu / 1e9` (summed, not USD)

**`result`** (exactly one, terminal)

Session totals and exit code.

```json
{
  "type": "result",
  "usage": {
    "premiumRequests": 0,
    "totalApiDurationMs": 1234,
    "sessionDurationMs": 2345
  },
  "sessionId": "uuid-...",
  "exitCode": 0
}
```

Fields populated in Usage:

- `provider_session_id` — from `sessionId`
- `raw` — `{premium_requests, total_api_duration_ms, session_duration_ms}`

If `exitCode` is nonzero, yields an ERROR chunk immediately.

### Asymmetry note

Copilot reports **AI credits**, not USD. The rate varies by model but hovers around 0.5–2.5 credits per short call. This is not directly comparable to Claude's USD cost without knowing the Copilot subscription's effective $/credit rate.

### Models available

On the account running Turnout, Copilot exposes models from four vendors:

- **Anthropic**: `claude-sonnet-5`, `claude-opus-5` (and others)
- **OpenAI**: `gpt-5.6-sol`, `gpt-5.4` (and others)
- **xAI**: `grok-4.5`
- **Google**: `gemini-3.7-flash`

All have been verified to actually answer on this account. The documented model list is wider; unverified names were left out of turnout.toml.

## OpenAI Codex CLI Adapter

**Binary:** `codex`  
**Command:** `codex exec --json --skip-git-repo-check --sandbox read-only --disable shell_tool --disable skill_search ... < prompt`

### Key flags

- `--skip-git-repo-check` — required outside a trusted git checkout (Turnout's neutral workdir never is one)
- `--sandbox read-only` — prevents write-mode tool use
- `--disable shell_tool` — explicitly disables the shell tool
- `--disable skill_search` — explicitly disables skill/web search

Why the double disabling? Codex is a full coding agent: even under `--sandbox read-only`, a plain "what's your model name" prompt was observed shelling out via `sed` to read a bundled skill file, then firing a real web search, purely because a skill description matched the prompt's keywords. Sandbox mode only prevents writes; it does not prevent tool use. The `--disable` flags gate whether the model even *knows about* the tools.

**No token-level streaming.** Unlike Claude Code's `stream-json`, Codex emits complete message bodies. There are no `item.updated` events; only `item.completed` events with the full `text` field. Time-to-first-byte lands close to the full response latency.

### Event stream

Events are line-delimited JSON describing the lifecycle of one turn: `thread.started` → `turn.started` → zero or more `item.completed` → `turn.completed` (or `turn.failed`).

**`thread.started`** (one at the very start)

Session initialization.

```json
{
  "type": "thread.started",
  "thread_id": "thread-abc123"
}
```

Fields:

- `provider_session_id` — from `thread_id` (used as session ID for this provider)

**`item.completed`** (zero or more per turn)

An item is a single unit of output: a message, reasoning, a tool call, a warning. The `type` field determines what was completed.

Agent message (the model's text response, always whole):

```json
{
  "type": "item.completed",
  "item": {
    "type": "agent_message",
    "text": "The complete response text"
  }
}
```

Yields: `Chunk(ChunkKind.TEXT, text)`

Reasoning (rare; never observed on this account because all models have `"default_reasoning_summary": "none"`):

```json
{
  "type": "item.completed",
  "item": {
    "type": "reasoning",
    "text": "Thinking about the problem..."
  }
}
```

Yields: `Chunk(ChunkKind.REASONING, text)`

Per-item warning (does not fail the turn):

```json
{
  "type": "item.completed",
  "item": {
    "type": "error",
    "message": "unknown model, using fallback metadata"
  }
}
```

Yields: `Chunk(ChunkKind.STATUS, "codex warning: ...")`

Tool use (shell_tool, skill_search, web_search, etc.—only reachable if --disable didn't cover every path):

```json
{
  "type": "item.completed",
  "item": {
    "type": "command_execution",
    "command": "grep hello file.txt"
  }
}
```

Yields: `Chunk(ChunkKind.STATUS, "codex command_execution: grep hello file.txt")`

**`turn.completed`** (one per turn, on success)

Final accounting.

```json
{
  "type": "turn.completed",
  "usage": {
    "input_tokens": 234,
    "output_tokens": 45,
    "cached_input_tokens": 0,
    "reasoning_output_tokens": 0,
    "cache_write_input_tokens": 0
  }
}
```

Fields populated in Usage:

- `input_tokens` — from `usage.input_tokens`
- `output_tokens` — from `usage.output_tokens`
- `cached_input_tokens` — from `usage.cached_input_tokens`
- `reasoning_tokens` — from `usage.reasoning_output_tokens`
- `raw` — `{cache_write_input_tokens}`

**`turn.failed` or `error`** (on failure)

Error event with nested JSON.

```json
{
  "type": "turn.failed",
  "error": "{\"error\": {\"message\": \"Model not found\"}}"
}
```

The real API error is wrapped in escaped JSON strings. The adapter unwraps it and yields: `Chunk(ChunkKind.ERROR, "Model not found")`

### Notable gaps

- **No cost reported.** The Codex CLI uses ChatGPT subscription auth, not metered API billing. The JSONL usage block has token counts only, never a dollar figure. `cost_usd` stays None.
- **No model confirmation.** The model actually used is never named in any event. `provider_model` remains whatever the caller specified, not confirmed back by the adapter.
- **Exit code trap.** Codex exits nonzero on fatal errors (including "model not found"), but the base CliAdapter's exit-code check would only fire if `saw_text == False`. The adapter parses `turn.failed` and `error` events into ERROR chunks *before* the base class checks, so the real error message surfaces even if a nonzero exit code would otherwise mask it. But: **if there is no `turn.failed` event, the exit code alone is the fallback.**

## OpenAI-compatible HTTP Adapter

**Base class:** Not a CliAdapter subclass (no subprocess to spawn)  
**Protocol:** OpenAI `/chat/completions` streaming (Server-Sent Events)

This adapter is a generic escape hatch for any OpenAI-compatible endpoint: xAI/Grok, Ollama, vLLM, LM Studio, OpenRouter, etc. One adapter class parameterized by `base_url` covers them all.

### Initialization

```python
adapter = OpenAiHttpAdapter(
    base_url="https://api.x.ai/v1",
    api_key_env="XAI_API_KEY",  # resolved at call time
    extra_headers={"User-Agent": "custom"},
    timeout_s=300,
)
```

**Why resolve the key at call time, not __init__ time?** Turnout can start with a target configured but its API key not yet exported. A later `export XAI_API_KEY=...` in the same shell must take effect without restarting Turnout or reconstructing the adapter.

### Probe

Calls `/models` (cheap, no completion) and returns `(True, "ok")` if it gets HTTP 200. If `api_key_env` is set but the env var is not, returns `(False, "{env_var_name} not set")` cleanly—never raises. This keeps a catalog probe of ten targets from exploding because one is optional.

### Stream

Calls `/chat/completions` with `stream: true` and `stream_options: {"include_usage": true}`. The latter is crucial: without it, OpenAI-compatible servers only attach usage numbers to the last SSE chunk *if explicitly asked*.

Reads Server-Sent Events line by line:

- Lines not starting with `data:` are skipped (comments, keep-alives)
- `data: [DONE]` terminates the stream
- `data: {...}` lines are parsed as JSON chunks

Each JSON chunk may contain:

```json
{
  "model": "grok-4",
  "choices": [{
    "delta": {
      "content": "text here",
      "reasoning_content": "thinking (vLLM/DeepSeek style)",
      "reasoning": "thinking (OpenRouter style)"
    }
  }],
  "usage": {
    "prompt_tokens": 123,
    "completion_tokens": 45,
    "prompt_tokens_details": {
      "cached_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 5000
    }
  }
}
```

Fields populated in Usage:

- `input_tokens` — from final chunk's `usage.prompt_tokens`
- `output_tokens` — from final chunk's `usage.completion_tokens`
- `cached_input_tokens` — from `usage.prompt_tokens_details.cached_tokens`
- `reasoning_tokens` — from `usage.completion_tokens_details.reasoning_tokens`
- `provider_model` — from first chunk with a `model` field
- `raw` — the full usage dict from the final chunk

**Reasoning field asymmetry:** Different servers use different key names. vLLM and DeepSeek use `reasoning_content`; OpenRouter uses `reasoning`. The adapter checks both.

**Error handling:** If status is not 200, buffers the error body fully (safe; API errors are small JSON) and yields `Chunk(ChunkKind.ERROR, f"HTTP {status}: {detail}")`. If the stream ends with no text and no usage, yields `Chunk(ChunkKind.ERROR, "stream ended with no content and no usage")` — catches dropped connections mid-body even when status is 200.

Always emits a terminal USAGE chunk (on success) or ERROR chunk (on failure). Unlike the CLI adapters, this adapter has no base class, so it is responsible for emitting that chunk itself.

## Comparison Table

| Adapter | Auth | Streaming | Cost reported | Session ID | Notes |
|---------|------|-----------|---------------|-----------|-------|
| `claude_cli` | CLI login | Yes (token-level) | **USD** | `session_id` | The only provider reporting real money. Prompt via stdin. |
| `copilot_cli` | CLI login | Yes (token-level) | **AI credits** (~0.5–2.5/call) | `sessionId` | Prompt in argv (no stdin mode). Token reduction flags drop ~22k overhead to ~2k. |
| `codex_cli` | CLI login (ChatGPT subscription) | No (complete messages) | None (subscription-based) | `thread_id` | Tool use disabled. Model name not confirmed back. |
| `openai_http` | Env var (call-time resolution) | Yes (SSE) | None (depends on provider) | None | Generic OpenAI-compatible endpoint. Works with xAI, Ollama, vLLM, LM Studio, OpenRouter. |

**Streaming nuance:** Both Claude Code and Copilot emit token-level streaming (you see text appear one token or few-token chunk at a time). Codex emits complete message bodies in one event, so time-to-first-byte is close to total latency.

## Adding a New Provider

To integrate a new provider: write an adapter subclass, wire it into `registry.py` (CLI adapters only — see below), and add a target for it in `turnout.toml`.

### Example: a new CLI provider

Suppose you have a CLI tool `mymodel` that takes a prompt on stdin and emits line-delimited JSON.

**File: `turnout/adapters/mymodel_cli.py`**

```python
from __future__ import annotations
from typing import Any
from ..domain import Chunk, ChunkKind, ExecRequest, Usage
from .base import CliAdapter, single_prompt

class MymodelCliAdapter(CliAdapter):
    name = "mymodel_cli"
    binary = "mymodel"
    probe_args = ["--version"]
    
    def build_argv(self, req: ExecRequest) -> list[str]:
        argv = [self.binary, "--streaming", "--json"]
        if req.target.model and req.target.model != "default":
            argv += ["--model", req.target.model]
        return argv
    
    def stdin_payload(self, req: ExecRequest) -> str | None:
        # Prompts via stdin (protect against ARG_MAX)
        return single_prompt(req.messages)
    
    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Chunk]:
        t = obj.get("type")
        u: Usage = state["usage"]
        
        if t == "text_chunk":
            if obj.get("text"):
                state["saw_text"] = True
                return [Chunk(ChunkKind.TEXT, obj["text"])]
            return []
        
        if t == "done":
            # Terminal event with usage
            usage = obj.get("usage") or {}
            u.input_tokens = usage.get("prompt_tokens")
            u.output_tokens = usage.get("completion_tokens")
            u.provider_session_id = obj.get("session_id")
            u.provider_model = obj.get("model")
            return []  # CliAdapter.stream() calls finalize() and emits the terminal USAGE chunk
        
        return []
```

**File: `turnout.toml` addition**

```toml
[[targets]]
id = "mymodel-default"
adapter = "mymodel_cli"
model = "default"
label = "MyModel (default)"
cost_tier = 3
speed_tier = 2
quality_tier = 3
tags = ["chat"]
notes = "My new provider."
```

**Register the adapter:**

Adapter wiring is not automatic. `registry.py`'s `build_adapters()` holds a fixed tuple of `(module, class_name, key)` triples — currently `claude_cli`/`ClaudeCliAdapter`, `copilot_cli`/`CopilotCliAdapter`, and `codex_cli`/`CodexCliAdapter` — and imports each by name. Add a `("mymodel_cli", "MymodelCliAdapter", "mymodel_cli")` entry to that tuple; a target's `adapter` key in `turnout.toml` refers to it by the third element. (HTTP providers are different: see the next section — those really are config-only, because `build_adapters()` constructs one `OpenAiHttpAdapter` per `http_providers` entry directly from the config, with no per-provider code at all.)

### Example: HTTP provider registration

For OpenAI-compatible endpoints, just add to `turnout.toml`:

```toml
[[http_providers]]
name        = "my_api"
base_url    = "https://api.example.com/v1"
api_key_env = "MY_API_KEY"

[[targets]]
id = "my-model"
adapter = "my_api"
model = "mymodel-7b"
label = "MyModel (my_api)"
cost_tier = 1
speed_tier = 1
quality_tier = 2
tags = ["chat", "local"]
notes = "A custom OpenAI-compatible endpoint."
```

No new code required—`OpenAiHttpAdapter` handles the protocol.

## Dead ends

**GitHub Models (`models.github.ai`)** is retired and returns HTTP 410. Do not bother adding it; it is gone.

## Trade-offs and Risks

### Subprocess-backed providers (claude_cli, copilot_cli, codex_cli)

**Startup latency.** Each call spawns a process. CLI startup adds 200–800ms depending on the tool and system load. This is unavoidable but measurable—factor it into time-sensitive routing decisions.

**CLI version drift.** Turnout does not pin or verify CLI versions anywhere — `probe()` just runs `--version` and reports back whatever string the binary prints, with no comparison against an expected value. If a user upgrades or downgrades a CLI, event formats may change silently. Non-JSON output lands in `state["nonjson"]` instead of crashing Turnout, but a CLI that renames or restructures a field `parse_line` depends on will just stop populating that field with no error.

**Subscription rate limits.** These tools are built for interactive use. The underlying APIs have rate limits; hammer them hard enough in a loop and you will hit a 429. Turnout itself has no concurrency limiter of its own — no semaphore, no per-adapter request cap — so nothing here stops a caller from firing enough concurrent requests to trip a provider's own limit.

**Non-interactive tool driven interactively.** These CLIs were not designed to be driven by scripts. They:

- Read `AGENTS.md` and `CLAUDE.md` from the working directory (hence the neutral cwd)
- Refuse to run in `-p` mode without an explicit tool-permission flag (hence Copilot's `--allow-all-tools`)
- Can exit nonzero on malformed or unexpected input; `CliAdapter.stream()` turns that into an ERROR chunk built from the stderr tail rather than crashing Turnout
- Dump progress/debug output to stderr (which Turnout logs but does not surface)

These are not showstoppers, but they create friction. If a future version of any CLI changes its behavior (interactive auth, new event types), Turnout may need patching.

### HTTP adapter (openai_http)

**Connection and timeout stability.** Remote endpoints can timeout, close connections, or return transient errors. The adapter has reasonable default timeouts (30s for `probe()`, 300s for a call), but there is no retry or backoff anywhere in this path: a failure becomes an ERROR chunk, and the executor's only response is to move to the next target in the router's `fallback_order` (if any) — immediately, with no delay. A flaky remote will add latency variance and, without a fallback target configured, a bare failure. Local endpoints (Ollama, vLLM) are much more stable.

**Key management.** API keys are resolved at call time from environment variables. If a key expires or is revoked mid-session, the next call fails cleanly. Turnout does not cache keys; each call re-fetches them.

## Debugging adapters

Each adapter logs:

- To `state["stderr"]` (CLI adapters only): every stderr line seen. On a nonzero exit with no text emitted, the last 8 lines become the ERROR chunk's own message text (not a `raw` field — there is no Usage object in that chunk). On a clean run where stderr wasn't empty, the last 5 lines are attached to the terminal Usage chunk's `raw["stderr_tail"]` instead — useful for catching warnings that didn't fail the call outright.
- To `state["nonjson"]` (CLI adapters only): a non-JSON stdout line, truncated to its first 500 characters; a line too large for the stream's read buffer is recorded as the fixed placeholder `"<oversized line dropped>"` instead of any of its actual content.
- To httpx exceptions (HTTP adapter): connection errors, timeouts, and HTTP errors, all yielded directly as ERROR chunks (this adapter has no `state["stderr"]`/`state["nonjson"]` bucket — it isn't a `CliAdapter`).

If a request fails outright, the ERROR chunk's `text` already carries the diagnosis (a CLI's stderr tail, or the HTTP adapter's unwrapped status/detail). A call that returned a *successful* Usage chunk but still seems off is the case for checking `raw["stderr_tail"]` — that's where quiet warnings surface.

For a CLI adapter, run the command manually in a neutral directory and check the event stream:

```bash
cd ~  # neutral cwd
claude -p --output-format stream-json --verbose ... < /tmp/prompt.txt | jq .
```

For an HTTP adapter, check the network logs:

```bash
curl -v -H "Authorization: Bearer $XAI_API_KEY" \
  https://api.x.ai/v1/chat/completions \
  -d '{"model": "grok-4", "messages": [...], "stream": true, "stream_options": {"include_usage": true}}' | jq .
```
