"""Shared server state: the app database, event bus, agent processes, and credentials.

Route modules import what they need from here, so ``main.py`` and the
routers in ``routes_*.py`` all work on the same objects.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from typing import Any

from fastapi import HTTPException

from . import alerts, cryozeta_bridge, guides, pricing
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
# No agent has survived a server stop, so nothing can still be working or paused.
store.settle_unfinished_sessions(("starting", "connecting", "initializing", "running", "paused"))
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
        "spending_limit_usd": saved.get("spending_limit_usd"),
    }


def experimental_settings() -> dict[str, Any]:
    """Experimental features. Turned on, reconstruction sessions started from now
    on get web search and page reading (reconstruction_agent/web_tools.py)."""

    saved = store.get_setting("experimental", {}) or {}
    return {"enabled": bool(saved.get("enabled"))}


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
    """The API key a session's provider needs. Ollama needs none; a run without
    a recorded provider ran on OpenAI."""

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


def driver_env(agent: str, *, guide: str | None = None, web_tools: bool = False) -> dict[str, str]:
    """Environment handed to an agent process: the API keys and the account
    that agent uses (CryoSPARC for reconstruction, CryoZeta for modeling).
    Empty values are not exported."""

    env = {"OPENAI_API_KEY": openai_key(), "ANTHROPIC_API_KEY": anthropic_key()}
    if agent == "reconstruction":
        cryosparc = cryosparc_settings()
        env.update(
            {
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
    if agent == "reconstruction":
        # Set either way, so a value exported where the server started cannot turn it on.
        env["CRYOPILOT_WEB_TOOLS"] = "1" if web_tools else "0"
    return env


# ------------------------------------------------------------------ usage
def parse_spending_limit(value: Any) -> float | None:
    """A spending limit from a form: dollars above zero, or nothing for no limit."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        limit = float(value)
    except (TypeError, ValueError):
        raise ValueError("The spending limit must be an amount in dollars.")
    if not 0 < limit < 1_000_000:
        raise ValueError("The spending limit must be more than $0.")
    return round(limit, 2)


def usage_view(session: dict[str, Any]) -> dict[str, Any]:
    """What a session has used so far, its estimated cost, and its spending limit."""

    rows = store.session_usage(session["id"])
    cost = sum(row["cost_usd"] for row in rows)
    unpriced = [row["model"] for row in rows if row["unpriced_requests"]]
    limit = (session.get("config") or {}).get("spending_limit_usd")
    return {
        "requests": sum(row["requests"] for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "cached_tokens": sum(row["cached_tokens"] for row in rows),
        "cache_write_tokens": sum(row["cache_write_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "cost_usd": cost,
        "models": rows,
        # Models without a listed price; the cost above leaves them out.
        "unpriced_models": unpriced,
        "limit_usd": limit,
        "over_limit": limit is not None and cost >= limit,
        "prices_as_of": pricing.PRICES_AS_OF,
    }


def require_within_limit(session: dict[str, Any]) -> None:
    """Refuse to start more work on a session that has reached its spending limit."""

    usage = usage_view(session)
    if usage["over_limit"]:
        raise api_error(
            409,
            f"This session has used about ${usage['cost_usd']:.2f} of its ${usage['limit_usd']:.2f} spending limit. Raise the limit to continue.",
            spending_limit=True,
        )


def apply_spending_limit(session: dict[str, Any]) -> None:
    """Tell a running agent where the session stands against its limit."""

    usage = usage_view(session)
    if usage["over_limit"]:
        processes.pause(
            session["id"], f"Spending limit reached: about ${usage['cost_usd']:.2f} of ${usage['limit_usd']:.2f}."
        )
    else:
        processes.resume(session["id"])


# ----------------------------------------------------------------- views
def session_runtime_view(session: dict[str, Any]) -> dict[str, Any]:
    view = dict(session)
    view["runtime"] = processes.status(session["id"])
    view["usage"] = usage_view(session)
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
        _alert_on_status(session_id, state, detail)
        return
    if name == "usage":
        _record_usage(session, event)
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
        _alert_on_turn_end(session_id, event.get("outcome"))
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


def _record_usage(session: dict[str, Any], event: dict[str, Any]) -> None:
    model = str(event.get("model") or "unknown")
    store.add_usage(session["id"], model, event, pricing.estimate(model, event, event.get("provider")))
    usage = usage_view(session)
    bus.publish(session["id"], {"type": "usage", "usage": usage})
    if usage["over_limit"]:
        apply_spending_limit(session)


# ----------------------------------------------------------------- alerts
_turn_started: dict[str, float] = {}


def _alert_settings() -> dict[str, Any]:
    return store.get_setting("alerts", {}) or {}


def _record_alert_result(result: dict[str, Any]) -> None:
    settings = _alert_settings()
    settings["last"] = result
    store.set_setting("alerts", settings)


alert_sender = alerts.AlertSender(settings=_alert_settings, secret=credentials.get, record=_record_alert_result)


def alerts_view() -> dict[str, Any]:
    return alerts.view(_alert_settings(), credentials.get, credentials.source)


def _final_message(session: dict[str, Any]) -> str:
    if session["kind"] == "modeling":
        answers = [event for event in store.list_modeling_events(session["id"]) if event["kind"] == "answer"]
        return str(answers[-1]["payload"].get("text") or "") if answers else ""
    harness_id = session.get("harness_session_id")
    if not harness_id:
        return ""
    try:
        return trace_reader.last_assistant_text(harness_id)
    except TraceError:
        return ""


def _queue_alert(session_id: str, event: str | None, *, detail: str = "", duration: float | None = None) -> None:
    """Queue an alert. Without an event, the agent's final message decides which it is."""

    def build() -> alerts.Alert | None:
        try:
            session = store.get_session(session_id)
            project = store.get_project(session["project_id"])
        except NotFound:
            return None
        message = detail or _final_message(session)
        name = event or alerts.turn_end_event(message, duration)
        if name is None:
            return None
        return alerts.Alert(
            event=name,
            session_id=session_id,
            session=session["title"],
            project=project["title"],
            message=message,
            duration=duration,
            cost_usd=usage_view(session)["cost_usd"],
        )

    alert_sender.enqueue(build, on_failure=lambda text: on_driver_log(session_id, "alerts", text))


def _alert_on_status(session_id: str, state: str, detail: str) -> None:
    if state == "running":
        _turn_started.setdefault(session_id, time.time())
    elif state == "paused":
        _queue_alert(session_id, "paused", detail=detail)
    elif state == "stopped":
        _turn_started.pop(session_id, None)
    elif state == "error" and not shutting_down.is_set():
        started = _turn_started.pop(session_id, None)
        _queue_alert(session_id, "failed", detail=detail, duration=time.time() - started if started else None)


def _alert_on_turn_end(session_id: str, outcome: Any) -> None:
    started = _turn_started.pop(session_id, None)
    if outcome == "completed":
        _queue_alert(session_id, None, duration=time.time() - started if started else None)


def on_driver_log(session_id: str, stream: str, text: str) -> None:
    line = store.append_console_line(session_id, stream, text)
    bus.publish(session_id, {"type": "console", "line": line})


processes = ProcessManager(on_event=on_driver_event, on_log=on_driver_log)
