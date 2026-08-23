"""Generic OpenAI-compatible HTTP adapter.

Unlike the CLI adapters, this one is not a subprocess wrapper: it speaks the
OpenAI `/chat/completions` streaming protocol directly over httpx. That single
protocol is now a lingua franca -- xAI/Grok, a local Ollama, vLLM, LM Studio,
and OpenRouter all speak it -- so one adapter class, parameterised by
`base_url`, covers every "real API key" provider Turnout will ever add
without a new file per vendor.

It satisfies the same duck-typed Adapter Protocol as CliAdapter subclasses
(`probe()` and `stream()`) but does not extend CliAdapter: there is no process
to spawn, no stderr to drain, and no argv to build, so inheriting from it would
mean overriding almost everything and keeping nothing.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx

from ..domain import Chunk, ChunkKind, ExecRequest, Message, Usage


def _messages_payload(messages: list[Message]) -> list[dict[str, str]]:
    # The OpenAI API takes a real messages array with its own system role and
    # cache/turn boundaries, so -- unlike the CLI adapters -- there is no
    # transcript to flatten. This is a straight pass-through.
    return [m.to_dict() for m in messages]


class OpenAiHttpAdapter:
    name = "openai_http"

    def __init__(
        self,
        base_url: str,
        api_key_env: str | None = None,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_s: float = 300,
    ):
        # No trailing slash bookkeeping scattered through the file.
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        # `api_key` is an explicit override (e.g. from a secrets manager); the
        # env var is the normal path. Neither is read here -- see _resolve_key.
        self._api_key_override = api_key
        self.extra_headers = extra_headers or {}
        self.timeout_s = timeout_s

    def _resolve_key(self) -> str | None:
        # Resolved at call time, never at __init__ time: Turnout can start
        # up with a target configured but its key not yet exported, and a
        # later `export FOO_API_KEY=...` in the same shell must take effect
        # without restarting anything or re-constructing the adapter.
        if self._api_key_override:
            return self._api_key_override
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None

    def _headers(self, key: str | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def probe(self) -> tuple[bool, str]:
        """Is this endpoint usable right now? Cheap -- lists models, no completion."""
        key = self._resolve_key()
        if self.api_key_env and not key:
            # This is the expected, steady-state condition for a provider the
            # user hasn't turned on yet: report it as a plain unavailability,
            # never raise, so a catalog probe of ten targets doesn't blow up
            # because one of them is optional.
            return False, f"{self.api_key_env} not set"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers(key))
            if resp.status_code == 200:
                return True, "ok"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except httpx.TimeoutException:
            return False, "probe timed out"
        except httpx.ConnectError as e:
            # The common case for a local endpoint (Ollama/vLLM not started
            # yet) -- worth a distinct message from a generic exception dump.
            return False, f"connection failed: {e}"
        except Exception as e:  # noqa: BLE001 - report any probe failure verbatim
            return False, f"{type(e).__name__}: {e}"

    async def stream(self, req: ExecRequest) -> AsyncIterator[Chunk]:
        key = self._resolve_key()
        if self.api_key_env and not key:
            yield Chunk(ChunkKind.ERROR, f"{self.api_key_env} not set")
            return

        body = {
            "model": req.target.model,
            "messages": _messages_payload(req.messages),
            "stream": True,
            # Without this the final usage numbers never arrive on a
            # streaming response -- OpenAI-compatible servers only attach
            # `usage` to the last SSE chunk when explicitly asked.
            "stream_options": {"include_usage": True},
        }
        usage = Usage()
        saw_text = False

        try:
            async with httpx.AsyncClient(timeout=req.timeout_s) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(key),
                    json=body,
                ) as resp:
                    if resp.status_code != 200:
                        # The body is only available after a read on a
                        # streamed response; non-200 bodies are small error
                        # JSON so buffering it fully here is fine.
                        raw = await resp.aread()
                        detail = raw.decode(errors="replace")[:500]
                        yield Chunk(
                            ChunkKind.ERROR,
                            f"HTTP {resp.status_code}: {detail}",
                            {"status_code": resp.status_code},
                        )
                        return

                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue  # SSE comments / keep-alives / blank separators
                        payload = line[len("data:") :].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except json.JSONDecodeError:
                            continue  # never surface a malformed SSE line as model output

                        model = obj.get("model")
                        if model:
                            usage.provider_model = model

                        choices = obj.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            if delta.get("content"):
                                saw_text = True
                                yield Chunk(ChunkKind.TEXT, delta["content"])
                            # Reasoning models split across servers on the key
                            # name: vLLM/DeepSeek-style APIs use
                            # `reasoning_content`, OpenRouter uses `reasoning`.
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if reasoning:
                                yield Chunk(ChunkKind.REASONING, reasoning)

                        # Present only on the final chunk when include_usage
                        # is set; earlier chunks omit the key entirely.
                        u = obj.get("usage")
                        if u:
                            usage.input_tokens = u.get("prompt_tokens")
                            usage.output_tokens = u.get("completion_tokens")
                            usage.cached_input_tokens = (u.get("prompt_tokens_details") or {}).get(
                                "cached_tokens"
                            )
                            usage.reasoning_tokens = (u.get("completion_tokens_details") or {}).get(
                                "reasoning_tokens"
                            )
                            usage.raw = u

        except httpx.TimeoutException:
            yield Chunk(ChunkKind.ERROR, f"timed out after {req.timeout_s:.0f}s")
            return
        except httpx.ConnectError as e:
            yield Chunk(ChunkKind.ERROR, f"connection failed: {e}")
            return
        except httpx.HTTPError as e:
            yield Chunk(ChunkKind.ERROR, f"{type(e).__name__}: {e}")
            return

        if not saw_text and not usage.input_tokens and not usage.output_tokens:
            # A stream that produced neither text nor usage is not a silent
            # success -- something upstream (empty choices, dropped
            # connection mid-body) went wrong even though the status was 200.
            yield Chunk(ChunkKind.ERROR, "stream ended with no content and no usage")
            return

        # This adapter has no base class to emit the terminal chunk for it,
        # so -- unlike the CLI adapters -- it is on us to do it exactly once.
        yield Chunk(ChunkKind.USAGE, "", {"usage": usage})
