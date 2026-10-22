"""Integrated terminal: shells on the server's computer, streamed over WebSocket.

Frames are JSON text. The browser sends ``{"type": "input", "data": ...}``
and ``{"type": "resize", "cols": N, "rows": M}``; the server sends
``{"type": "output", "data": ...}`` and, when the shell ends,
``{"type": "exit", "code": N}``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from . import files as file_roots
from .state import api_error, require_project
from .terminal import Subscriber, Terminal, TerminalError, TerminalLimitError, manager

router = APIRouter()

CLOSE_NOT_FOUND = 4404
CLOSE_TOO_SLOW = 1013  # "try again later": the page reconnects and replays the scrollback


def shutdown() -> None:
    """Close every terminal when the server stops."""

    manager.shutdown()


def _terminal_location(body: dict[str, Any]) -> tuple[Path, str, str | None, str | None]:
    """Working directory, title, project id, and root for a new terminal."""

    project_id = str(body.get("project_id") or "").strip() or None
    root = str(body.get("root") or "").strip() or None
    if project_id is None or root == "home":
        return Path.home(), "Home", None, "home" if root == "home" else None
    project = require_project(project_id)
    file_roots.ensure_project_dirs(project["slug"])
    root = root or "modeling"
    try:
        cwd = file_roots.root_path(project["slug"], root)
    except ValueError as exc:
        raise api_error(400, str(exc))
    title = file_roots.ROOT_LABELS.get(root)
    if title is None:
        title = next((item["label"] for item in file_roots.project_roots(project) if item["key"] == root), root)
    return cwd, title, project["id"], root


@router.get("/api/terminals")
def list_terminals() -> dict[str, Any]:
    return {"terminals": manager.list()}


@router.post("/api/terminals")
async def create_terminal(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    # Finding a CryoSPARC folder or starting a shell can take a moment; keep it off the event loop.
    cwd, title, project_id, root = await run_in_threadpool(_terminal_location, body)
    try:
        terminal = await run_in_threadpool(
            manager.create,
            cwd=cwd,
            title=title,
            project_id=project_id,
            root=root,
            cols=body.get("cols"),
            rows=body.get("rows"),
        )
    except TerminalLimitError as exc:
        raise api_error(409, str(exc))
    except (TerminalError, OSError) as exc:
        raise api_error(500, f"Could not start a shell: {exc}")
    return terminal.view()


@router.delete("/api/terminals/{terminal_id}")
def close_terminal(terminal_id: str) -> dict[str, Any]:
    if not manager.close(terminal_id):
        raise api_error(404, "Terminal not found")
    return {"ok": True}


# --------------------------------------------------------------- websocket
async def _send_output(websocket: WebSocket, subscriber: Subscriber) -> None:
    """Forward the shell's output, merging chunks that arrived while the socket was busy."""

    while True:
        kind, payload = await subscriber.queue.get()
        if kind == "output":
            parts = [payload]
            while not subscriber.queue.empty():
                next_kind, next_payload = subscriber.queue.get_nowait()
                if next_kind != "output":
                    kind, payload = next_kind, next_payload
                    break
                parts.append(next_payload)
            text = "".join(parts)
            subscriber.taken(len(text))
            await websocket.send_json({"type": "output", "data": text})
            if kind == "output":
                continue
        if kind == "exit":
            await websocket.send_json({"type": "exit", "code": payload})
            await websocket.close(code=1000)
            return
        if kind == "overflow":
            await websocket.close(code=CLOSE_TOO_SLOW, reason="Output arrived faster than the page could show it")
            return


async def _receive_input(websocket: WebSocket, terminal: Terminal) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        text = message.get("text")
        if text is None and message.get("bytes") is not None:
            text = message["bytes"].decode("utf-8", errors="replace")
        try:
            frame = json.loads(text or "")
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        kind = frame.get("type")
        if kind == "input" and isinstance(frame.get("data"), str):
            # A large paste can fill the PTY's input buffer; do not block the event loop on it.
            await asyncio.to_thread(terminal.write, frame["data"])
        elif kind == "resize":
            terminal.resize(frame.get("cols"), frame.get("rows"))


@router.websocket("/api/terminals/{terminal_id}/ws")
async def terminal_socket(websocket: WebSocket, terminal_id: str) -> None:
    await websocket.accept()
    terminal = manager.get(terminal_id)
    if terminal is None:
        await websocket.close(code=CLOSE_NOT_FOUND, reason="Terminal not found")
        return
    subscriber, backlog = terminal.subscribe(asyncio.get_running_loop())
    try:
        if backlog:
            await websocket.send_json({"type": "output", "data": backlog})
        if not terminal.alive:
            await websocket.send_json({"type": "exit", "code": terminal.exit_code})
            await websocket.close(code=1000)
            return
        async with anyio.create_task_group() as group:

            async def run_until_either_ends(work, *args) -> None:
                try:
                    await work(*args)
                except (WebSocketDisconnect, RuntimeError, OSError):
                    pass  # the page went away; the terminal keeps running
                finally:
                    group.cancel_scope.cancel()

            group.start_soon(run_until_either_ends, _send_output, websocket, subscriber)
            group.start_soon(run_until_either_ends, _receive_input, websocket, terminal)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        terminal.unsubscribe(subscriber)
