"""In-process publish/subscribe bus used to fan events out to SSE clients."""

from __future__ import annotations

import queue
import threading
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._last_status: dict[str, dict[str, Any]] = {}

    def subscribe(self, key: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.setdefault(key, []).append(q)
        return q

    def unsubscribe(self, key: str, q: queue.Queue) -> None:
        with self._lock:
            subscribers = self._subscribers.get(key) or []
            if q in subscribers:
                subscribers.remove(q)
            if not subscribers:
                self._subscribers.pop(key, None)

    def publish(self, key: str, event: dict[str, Any]) -> None:
        if event.get("type") == "status":
            with self._lock:
                self._last_status[key] = event
        with self._lock:
            subscribers = list(self._subscribers.get(key) or [])
        for q in subscribers:
            q.put(event)

    def last_status(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._last_status.get(key)
