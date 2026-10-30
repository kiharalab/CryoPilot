"""Shared server state: the app database, event bus, agent processes, and credentials.

Route modules import what they need from here, so ``main.py`` and the
routers in ``routes_*.py`` all work on the same objects.
"""

from __future__ import annotations

import os
import platform
import threading
from typing import Any

from fastapi import HTTPException

from . import cryozeta_bridge, guides
from .config import (
    APP_DB_PATH,
    DEFAULT_MODELING_MODEL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_RECONSTRUCTION_MODEL,
    HARNESS_SESSION_DB,
    ensure_directories,
)
from .credentials import Credentials
from .events import EventBus
from .processes import ProcessManager
from .store import AppStore, NotFound
from .trace import HarnessTraceReader, TraceError

ensure_directories()

store = AppStore(APP_DB_PATH)
bus = EventBus()
trace_reader = HarnessTraceReader(HARNESS_SESSION_DB)
credentials = Credentials()
# Set by serve.py when the server starts to shut down, so long-lived streams
# end on their own instead of holding the shutdown up.
shutting_down = threading.Event()

MODELING_EVENT_KINDS = {"reasoning", "tool_call", "tool_output", "answer", "error"}
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
# The reconstruction agent also runs without reasoning ("none").
RECONSTRUCTION_EFFORTS = ("none", *REASONING_EFFORTS)
DEFAULT_MODELING_EFFORT = "high"
DEFAULT_MODELING_MAX_TURNS = 500
SYSTEM = platform.system()
# The reconstruction agent reads and writes CryoSPARC project files directly,
# so it runs only on the Linux machine where CryoSPARC is installed.
# CRYOPILOT_ALLOW_RECONSTRUCTION=1 allows it elsewhere, for development against
# a CryoSPARC instance whose project folders are mounted locally.
RECONSTRUCTION_SUPPORTED = SYSTEM == "Linux" or os.environ.get("CRYOPILOT_ALLOW_RECONSTRUCTION") == "1"


# ---------------------------------------------------------------- helpers
def api_error(status: int, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"message": message, **extra})


def require_project(project_id: str) -> dict[str, Any]:
    try:
        return store.get_project(project_id)
    except NotFound as exc:
        raise api_error(404, str(exc))


def require_session(session_id: str) -> dict[str, Any]:
    try:
        return store.get_session(session_id)
    except NotFound as exc:
        raise api_error(404, str(exc))


# --------------------------------------------------------------- settings
def defaults() -> dict[str, Any]:
    saved = store.get_setting("defaults", {}) or {}
    return {
        "reconstruction_model": saved.get("reconstruction_model") or DEFAULT_RECONSTRUCTION_MODEL,
        "reconstruction_effort": saved.get("reconstruction_effort") or DEFAULT_REASONING_EFFORT,
        "modeling_model": saved.get("modeling_model") or DEFAULT_MODELING_MODEL,
        "modeling_effort": saved.get("modeling_effort") or DEFAULT_MODELING_EFFORT,
        "modeling_max_turns": int(saved.get("modeling_max_turns") or DEFAULT_MODELING_MAX_TURNS),
    }


def processing_guide() -> str:
    return guides.normalize(store.get_setting("processing_guide", guides.DEFAULT_GUIDE))


def openai_key() -> str:
    return credentials.get("openai_api_key")


def require_openai_key() -> None:
    if not openai_key():
        raise api_error(400, "Add your OpenAI API key in Settings before starting an agent.", setup="openai")


def anthropic_key() -> str:
    return credentials.get("anthropic_api_key")


def require_model_key(provider: str | None) -> None:
    """The API key a reconstruction session's provider needs. Ollama needs none;
    a run without a recorded provider ran on OpenAI."""

    if provider == "ollama":
        return
    if provider == "anthropic":
        if not anthropic_key():
            raise api_error(400, "Add your Anthropic API key in Settings to run a Claude model.", setup="anthropic")
        return
    require_openai_key()


def cryosparc_settings() -> dict[str, Any]:
    saved = store.get_setting("cryosparc", {}) or {}
    url = saved.get("url") or os.environ.get("CRYOSPARC_BASE_URL") or "http://localhost:61000"
    email = saved.get("email") or os.environ.get("CRYOSPARC_EMAIL") or ""
    return {
        "url": url,
        "email": email,
        "password_set": credentials.is_set("cryosparc_password"),
        "verified_at": saved.get("verified_at"),
    }


def cryosparc_credentials(required: bool = True) -> tuple[str, str, str] | None:
    settings = cryosparc_settings()
    password = credentials.get("cryosparc_password")
    if not settings["email"] or not password:
        if not required:
            return None
        raise api_error(400, "Sign in to CryoSPARC in Settings first.", setup="cryosparc")
    return settings["url"], settings["email"], password


def cryozeta_settings() -> dict[str, Any]:
    saved = store.get_setting("cryozeta", {}) or {}
    base_url = saved.get("base_url") or os.environ.get("CRYOZETA_BASE_URL") or cryozeta_bridge.DEFAULT_BASE_URL
    username = saved.get("username") or os.environ.get("CRYOZETA_USERNAME") or ""
    return {
        "base_url": base_url,
        "username": username,
        "password_set": credentials.is_set("cryozeta_password"),
        "verified_at": saved.get("verified_at"),
    }


def cryozeta_credentials() -> tuple[str, str, str]:
    settings = cryozeta_settings()
    password = credentials.get("cryozeta_password")
    if not settings["username"] or not password:
        raise api_error(400, "Sign in to the EM server (CryoZeta) in Settings first.", setup="cryozeta")
    return settings["base_url"], settings["username"], password


def driver_env(agent: str, *, guide: str | None = None) -> dict[str, str]:
    """Environment handed to an agent process: the API keys and the account
    that agent uses (CryoSPARC for reconstruction, CryoZeta for modeling).
    Empty values are not exported."""

    env = {"OPENAI_API_KEY": openai_key()}
    if agent == "reconstruction":
        cryosparc = cryosparc_settings()
        env.update(
            {
                "ANTHROPIC_API_KEY": anthropic_key(),
                "CRYOSPARC_BASE_URL": cryosparc["url"],
                "CRYOSPARC_EMAIL": cryosparc["email"],
                "CRYOSPARC_PASSWORD": credentials.get("cryosparc_password"),
            }
        )
    else:
        cryozeta = cryozeta_settings()
        env.update(
            {
                "CRYOZETA_BASE_URL": cryozeta["base_url"],
                "CRYOZETA_USERNAME": cryozeta["username"],
                "CRYOZETA_PASSWORD": credentials.get("cryozeta_password"),
            }
        )
    if guide:
        env["CRYOEM_GUIDE"] = guides.normalize(guide)
    return env


# ----------------------------------------------------------------- views
def session_runtime_view(session: dict[str, Any]) -> dict[str, Any]:
    view = dict(session)
    view["runtime"] = processes.status(session["id"])
    if session["kind"] == "reconstruction" and session.get("harness_session_id"):
        try:
            harness = trace_reader.session(session["harness_session_id"])
        except TraceError as exc:
            harness = {"error": str(exc)}
        view["harness"] = harness
    else:
        view["harness"] = None
    return view


# --------------------------------------------------------- driver callbacks
def on_driver_event(session_id: str, event: dict[str, Any]) -> None:
    name = event.get("event")
    try:
        session = store.get_session(session_id)
    except NotFound:
        return
    if name == "status":
        state = str(event.get("state") or "")
        detail = str(event.get("detail") or "")
        updates: dict[str, Any] = {"status": state}
        if state == "error":
            updates["last_error"] = detail
        store.update_session(session_id, **updates)
        bus.publish(session_id, {"type": "status", "state": state, "detail": detail, "running": state not in {"stopped", "error"}})
        return
    if name == "session":
        store.update_session(session_id, harness_session_id=event.get("harness_session_id"))
        bus.publish(session_id, {"type": "session", "session": session_runtime_view(store.get_session(session_id))})
        return
    if name == "scope":
        config = dict(session.get("config") or {})
        config["project_uid"] = event.get("project_uid")
        config["workspace_uid"] = event.get("workspace_uid")
        store.update_session(session_id, config=config)
        bus.publish(session_id, {"type": "session", "session": session_runtime_view(store.get_session(session_id))})
        return
    if name == "delta":
        bus.publish(session_id, {"type": "delta", "channel": event.get("channel"), "text": event.get("text", "")})
        return
    if name == "turn_end":
        bus.publish(session_id, {"type": "turn_end", "outcome": event.get("outcome")})
        return
    if name == "user_turn_accepted":
        return
    if session["kind"] == "modeling" and name in MODELING_EVENT_KINDS:
        payload = {key: value for key, value in event.items() if key != "event"}
        record = store.append_modeling_event(session_id, name, payload)
        bus.publish(session_id, {"type": "event", "event": record})
        return
    if name == "error":
        bus.publish(session_id, {"type": "error", "message": event.get("message")})


def on_driver_log(session_id: str, stream: str, text: str) -> None:
    line = store.append_console_line(session_id, stream, text)
    bus.publish(session_id, {"type": "console", "line": line})


processes = ProcessManager(on_event=on_driver_event, on_log=on_driver_log)
