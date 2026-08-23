"""The generic OpenAI-compatible adapter, exercised against a local fake server.

This is the escape hatch that lets Turnout reach any endpoint with an API
key -- xAI, Ollama, vLLM, OpenRouter -- so it needs to be correct even though
none of those are configured on this machine.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from turnout.adapters.openai_http import OpenAiHttpAdapter
from turnout.domain import ChunkKind, ExecRequest, Message, Target

PORT = 8898
BASE = f"http://127.0.0.1:{PORT}/v1"


@pytest.fixture(scope="module")
def fake_server():
    script = Path(__file__).parent / "fake_openai_server.py"
    proc = subprocess.Popen(
        [sys.executable, str(script), "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        try:
            if httpx.get(f"{BASE}/models", timeout=0.5).status_code == 200:
                break
        except Exception:  # noqa: BLE001 - server still binding
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("fake OpenAI server did not start")
    yield BASE
    proc.kill()
    proc.wait(timeout=5)


def exec_req(prompt: str = "hi") -> ExecRequest:
    return ExecRequest("r1", Target("fake", "openai_http", "fake-model", "Fake"),
                       [Message("user", prompt)], "s1", timeout_s=20)


async def drain(adapter, req):
    text, usage, errors = "", None, []
    async for c in adapter.stream(req):
        if c.kind is ChunkKind.TEXT:
            text += c.text
        elif c.kind is ChunkKind.USAGE:
            usage = c.data.get("usage")
        elif c.kind is ChunkKind.ERROR:
            errors.append(c.text)
    return text, usage, errors


async def test_probe_reports_missing_key_without_raising():
    a = OpenAiHttpAdapter(base_url="https://api.example.invalid/v1",
                          api_key_env="DEFINITELY_NOT_SET_KEY")
    ok, detail = await a.probe()
    assert ok is False and "DEFINITELY_NOT_SET_KEY" in detail


async def test_probe_succeeds_against_a_live_endpoint(fake_server):
    ok, _ = await OpenAiHttpAdapter(base_url=fake_server).probe()
    assert ok is True


async def test_streams_text_and_reports_usage(fake_server):
    text, usage, errors = await drain(OpenAiHttpAdapter(base_url=fake_server), exec_req())
    assert not errors
    assert text
    assert usage is not None
    assert usage.input_tokens and usage.output_tokens
    assert usage.provider_model


async def test_server_error_becomes_a_clean_error_chunk(fake_server):
    """An upstream 500 must not escape as an exception -- the executor needs a
    chunk it can record and fall back from."""
    _, _, errors = await drain(OpenAiHttpAdapter(base_url=fake_server), exec_req("TRIGGER_500"))
    assert errors and "500" in errors[0]


async def test_unreachable_host_becomes_a_clean_error_chunk():
    a = OpenAiHttpAdapter(base_url="http://127.0.0.1:1/v1")
    _, _, errors = await drain(a, exec_req())
    assert errors
