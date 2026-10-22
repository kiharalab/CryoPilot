"""JSON-lines protocol shared by the agent drivers and the app server.

Server -> driver (stdin, one JSON object per line):
    {"op": "message", "content": "..."}     append a user turn and run the agent
    {"op": "shutdown"}                        exit cleanly after the current turn

Driver -> server (stdout, one JSON object per line):
    {"event": "status", "state": "...", "detail": "..."}
    {"event": "session", "harness_session_id": "..."}          (reconstruction)
    {"event": "reasoning" | "tool_call" | "tool_output" | "answer" | "user", ...}  (modeling)
    {"event": "delta", "channel": "reasoning" | "answer", "text": "..."}  (modeling, not persisted)
    {"event": "final", "text": "..."}
    {"event": "error", "message": "..."}

Everything the drivers print through ``print`` goes to stderr so that stdout
stays a clean event stream; the server records stderr as the console log.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from typing import Any


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


class CommandReader:
    """Background reader that turns stdin lines into a queue of commands."""

    def __init__(self) -> None:
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
            if isinstance(command, dict):
                self.queue.put(command)
        self.queue.put(None)

    def next(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return {"op": "noop"}
