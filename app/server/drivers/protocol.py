"""JSON-lines protocol shared by the agent drivers and the app server.

Server -> driver (stdin, one JSON object per line):
    {"op": "message", "content": "..."}     append a user turn and run the agent
    {"op": "shutdown"}                        exit cleanly after the current turn
    {"op": "pause", "detail": "..."}          hold the next model request (read at once, even during a turn)
    {"op": "resume"}                          let model requests go again

Driver -> server (stdout, one JSON object per line):
    {"event": "status", "state": "...", "detail": "..."}
    {"event": "session", "harness_session_id": "..."}          (reconstruction)
    {"event": "reasoning" | "tool_call" | "tool_output" | "answer" | "user", ...}  (modeling)
    {"event": "delta", "channel": "reasoning" | "answer", "text": "..."}  (modeling, not persisted)
    {"event": "final", "text": "..."}
    {"event": "usage", "model": "...", "input_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
     "cache_write_long_tokens": 0, "output_tokens": 0}          after each model response
    {"event": "error", "message": "..."}

Everything the drivers print through ``print`` goes to stderr so that stdout
stays a clean event stream; the server records stderr as the console log.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
from typing import Any, Callable


class Emitter:
    """Writes events to the original stdout file descriptor."""

    def __init__(self) -> None:
        self._stream = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def emit(self, event: str, **payload: Any) -> None:
        record = {"event": event, **payload}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def redirect_stdout_to_stderr() -> None:
    sys.stdout = sys.stderr


class RunGate:
    """Holds model requests while the app has paused the run.

    A run pauses between model requests, so a tool or CryoSPARC job under way
    finishes first and the agent keeps its whole state. Stopping the turn still
    works during a hold: it raises in the blocking wait and cancels the async one.
    """

    def __init__(self, status: Callable[[str, str], None]) -> None:
        self._status = status
        self._open = threading.Event()
        self._open.set()
        self._detail = ""

    def pause(self, detail: str = "") -> None:
        self._detail = detail
        self._open.clear()

    def resume(self) -> None:
        self._open.set()

    def hold(self) -> None:
        if self._open.is_set():
            return
        self._status("paused", self._detail)
        while not self._open.wait(0.25):
            pass
        self._status("running", "Agent is working")

    async def hold_async(self) -> None:
        if self._open.is_set():
            return
        self._status("paused", self._detail)
        while not self._open.is_set():
            await asyncio.sleep(0.25)
        self._status("running", "Agent is working")


class CommandReader:
    """Background reader that turns stdin lines into a queue of commands.

    Pause and resume go straight to the gate, because the queue is read only
    between turns.
    """

    def __init__(self, gate: RunGate | None = None) -> None:
        self._gate = gate
        self.queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="stdin-reader", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                command = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(command, dict):
                continue
            if self._gate is not None and command.get("op") == "pause":
                self._gate.pause(str(command.get("detail") or ""))
            elif self._gate is not None and command.get("op") == "resume":
                self._gate.resume()
            else:
                self.queue.put(command)
        self.queue.put(None)

    def next(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return {"op": "noop"}
