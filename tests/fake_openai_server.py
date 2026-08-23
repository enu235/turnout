"""Minimal fake OpenAI-compatible server for exercising OpenAiHttpAdapter.

Serves just enough of the API surface -- GET /v1/models and a streaming POST
/v1/chat/completions -- to test the adapter's SSE parsing and error handling
without hitting a real provider or requiring Ollama to be running. Kept in
the repo (not deleted after use) because any future work on openai_http.py
needs the same fixture.

Run directly: `python tests/fake_openai_server.py` (binds 127.0.0.1:8899).

Special-case trigger for error-path testing: a request whose last user
message is exactly "TRIGGER_500" gets a 500 response instead of a stream.
"""

from __future__ import annotations

import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

MODEL = "fake-gpt"


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def _chunks():
    base = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL,
    }
    # A couple of reasoning deltas ahead of the visible text, the way a
    # reasoning-capable OpenAI-compatible model would stream them.
    yield _sse({**base, "choices": [{"index": 0, "delta": {"reasoning_content": "thinking... "}}]})
    for word in ["1", "\n2", "\n3"]:
        yield _sse({**base, "choices": [{"index": 0, "delta": {"content": word}}]})
    yield _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    # Final chunk: empty choices + usage, per stream_options.include_usage.
    yield _sse(
        {
            **base,
            "choices": [],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 4},
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
        }
    )
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    if last_user.strip() == "TRIGGER_500":
        return JSONResponse(status_code=500, content={"error": {"message": "synthetic failure for testing"}})
    return StreamingResponse(_chunks(), media_type="text/event-stream")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    # The test suite binds its own port so a manually-started instance on the
    # default port does not collide with a test run.
    ap.add_argument("--port", type=int, default=8899)
    uvicorn.run(app, host="127.0.0.1", port=ap.parse_args().port, log_level="warning")
