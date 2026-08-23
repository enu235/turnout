"""Shared plumbing for adapters that drive a local CLI as a subprocess.

Every CLI-backed provider here follows the same shape: spawn a process, read
line-delimited JSON off stdout, translate each line into a Chunk, and emit one
terminal USAGE (or ERROR) chunk. The differences between providers are entirely
in `build_argv` and `parse_line`, so those are the only two things a subclass
writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from typing import Any

from ..domain import Chunk, ChunkKind, ExecRequest, Message, Usage


def flatten_messages(messages: list[Message]) -> str:
    """CLIs take a single prompt string, not a message array.

    We render the conversation into a plain transcript. It is lossy compared to
    a real chat API -- there is no separate system role and no cache boundary --
    so Turnout records the original messages in the DB, not this rendering.
    """
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(f"[System instructions]\n{m.content}")
        elif m.role == "user":
            parts.append(f"[User]\n{m.content}")
        elif m.role == "assistant":
            parts.append(f"[Assistant]\n{m.content}")
    parts.append("[Assistant]")
    return "\n\n".join(parts)


def single_prompt(messages: list[Message]) -> str:
    """Fast path: a lone user turn needs no transcript framing."""
    non_system = [m for m in messages if m.role != "system"]
    if len(non_system) == 1 and non_system[0].role == "user":
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        body = non_system[0].content
        return f"{system}\n\n{body}".strip() if system else body
    return flatten_messages(messages)


class CliAdapter:
    """Base class. Subclasses set `name`, `binary`, and implement the two hooks."""

    name: str = "cli"
    binary: str = ""
    probe_args: list[str] = ["--version"]

    def __init__(self, workdir: str | None = None, extra_env: dict[str, str] | None = None):
        # A neutral cwd matters: these CLIs read AGENTS.md/CLAUDE.md from the
        # working directory, which would silently change the prompt.
        self.workdir = os.path.expanduser(workdir or "~")
        self.extra_env = extra_env or {}
        if not os.path.isdir(self.workdir):
            self.workdir = os.path.expanduser("~")

    # -- hooks -------------------------------------------------------------

    def build_argv(self, req: ExecRequest) -> list[str]:
        raise NotImplementedError

    def parse_line(self, obj: dict[str, Any], state: dict[str, Any]) -> list[Chunk]:
        """Translate one parsed JSON line into zero or more Chunks.

        `state` is a scratch dict that lives for the duration of one execution;
        use it to accumulate usage seen across several lines.
        """
        raise NotImplementedError

    def finalize(self, state: dict[str, Any]) -> Usage:
        return state.get("usage") or Usage()

    def stdin_payload(self, req: ExecRequest) -> str | None:
        """Return a string to write to stdin, or None to pass the prompt in argv."""
        return None

    # -- runtime -----------------------------------------------------------

    async def probe(self) -> tuple[bool, str]:
        path = shutil.which(self.binary)
        if not path:
            return False, f"{self.binary} not found on PATH"
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, *self.probe_args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            head = out.decode(errors="replace").strip().splitlines()
            return proc.returncode == 0, (head[0] if head else path)
        except TimeoutError:
            return False, "probe timed out"
        except Exception as e:  # noqa: BLE001 - report any launch failure verbatim
            return False, f"{type(e).__name__}: {e}"

    async def stream(self, req: ExecRequest) -> AsyncIterator[Chunk]:
        argv = self.build_argv(req)
        stdin_data = self.stdin_payload(req)
        env = {**os.environ, **self.extra_env}
        state: dict[str, Any] = {"usage": Usage(), "stderr": []}

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                env=env,
            )
        except FileNotFoundError:
            # This is raised both for a missing binary and for a missing cwd,
            # and conflating the two sends you hunting for the wrong bug.
            if shutil.which(self.binary) is None:
                yield Chunk(ChunkKind.ERROR, f"{self.binary} not found on PATH")
            else:
                yield Chunk(ChunkKind.ERROR, f"working directory does not exist: {self.workdir}")
            return

        async def drain_stderr() -> None:
            assert proc.stderr is not None
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    state["stderr"].append(line)

        stderr_task = asyncio.create_task(drain_stderr())

        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data.encode())
            await proc.stdin.drain()
            proc.stdin.close()

        try:
            assert proc.stdout is not None
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=req.timeout_s)
                except TimeoutError:
                    proc.kill()
                    yield Chunk(ChunkKind.ERROR, f"timed out after {req.timeout_s:.0f}s")
                    return
                except ValueError:
                    # readline() raises when a single line exceeds the stream
                    # limit; these CLIs can emit very large JSON lines.
                    state.setdefault("nonjson", []).append("<oversized line dropped>")
                    continue
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # Not every line is JSON (banners, progress). Keep it for
                    # diagnosis but never surface it as model output.
                    state.setdefault("nonjson", []).append(line[:500])
                    continue
                for chunk in self.parse_line(obj, state):
                    yield chunk

            rc = await proc.wait()
            await stderr_task
            if rc != 0 and not state.get("saw_text"):
                err = "\n".join(state["stderr"][-8:]) or f"exit code {rc}"
                yield Chunk(ChunkKind.ERROR, err, {"exit_code": rc})
                return
            usage = self.finalize(state)
            if state["stderr"]:
                usage.raw.setdefault("stderr_tail", state["stderr"][-5:])
            yield Chunk(ChunkKind.USAGE, "", {"usage": usage})
        except asyncio.CancelledError:
            proc.kill()
            raise
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
