"""Tells people about a run through a webhook or email when they are not looking at the app.

Nothing is sent until someone sets a destination in Settings. An alert says
what happened, to which session and project, how long it took, and what it
cost; the agent's last message goes along only when "include_message" is on,
because it leaves this computer. Sending happens on its own thread, so a slow
mail server never holds up the agents' events.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Callable

import certifi

log = logging.getLogger(__name__)

WEBHOOK_SECRET = "alert_webhook_url"
SMTP_SECRET = "alert_smtp_password"

# Each alert a run can send, and how its subject line ends.
EVENTS: dict[str, str] = {
    "finished": "finished",
    "question": "is waiting for your answer",
    "failed": "stopped because of an error",
    "paused": "paused at its spending limit",
}
SECURITY_OPTIONS = ("starttls", "ssl", "none")
DEFAULT_SMTP_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
# A short exchange with someone at the screen is not worth an alert; a run is.
FINISHED_MINIMUM_SECONDS = 120
MESSAGE_LIMIT = 500
SEND_TIMEOUT_SECONDS = 20
RETRY_DELAYS_SECONDS = (15, 90)
DISCORD_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}


class AlertSettingsError(ValueError):
    pass


@dataclass
class Alert:
    event: str
    session_id: str = ""
    session: str = ""
    project: str = ""
    message: str = ""
    duration: float | None = None
    cost_usd: float | None = None


def turn_end_event(final_message: str, duration: float | None) -> str | None:
    """Which alert a finished turn calls for: a question, the end of a run, or none.

    A model that wants an answer ends its turn like one that is done, so the
    last paragraph decides. A question always alerts; a turn that finished
    quickly had someone watching it.
    """

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", final_message or "") if part.strip()]
    last = paragraphs[-1] if paragraphs else ""
    if "?" in last or re.search(r"\blet me know\b", last, re.IGNORECASE):
        return "question"
    if duration is not None and duration < FINISHED_MINIMUM_SECONDS:
        return None
    return "finished"


def _duration_text(seconds: float) -> str:
    total = max(0, round(seconds))
    if total < 60:
        return f"{total} s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def render(alert: Alert, settings: dict[str, Any]) -> tuple[str, str]:
    """The subject and text of an alert."""

    title = alert.session or "A session"
    subject = f"CryoPilot: {title} {EVENTS.get(alert.event, alert.event)}"
    lines = [subject]
    if alert.project:
        lines.append(f"Project: {alert.project}")
    if alert.duration is not None:
        lines.append(f"Time: {_duration_text(alert.duration)}")
    if alert.cost_usd:
        lines.append(f"Estimated cost: ${alert.cost_usd:.2f}")
    if settings.get("include_message") and alert.message.strip():
        text = alert.message.strip()
        if len(text) > MESSAGE_LIMIT:
            text = text[-MESSAGE_LIMIT:].lstrip() if alert.event == "question" else text[:MESSAGE_LIMIT].rstrip()
            text = f"…{text}" if alert.event == "question" else f"{text}…"
        lines += ["", text]
    app_url = str(settings.get("app_url") or "").rstrip("/")
    if app_url and alert.session_id:
        lines += ["", f"{app_url}/?session={urllib.parse.quote(alert.session_id)}"]
    return " ".join(subject.split()), "\n".join(lines)


# ------------------------------------------------------------------ channels
def _tls() -> ssl.SSLContext:
    # The packaged program cannot rely on finding this computer's certificates.
    return ssl.create_default_context(cafile=certifi.where())


def _plain_error(exc: BaseException) -> str:
    """Why sending failed, without the webhook address, which works like a password."""

    if isinstance(exc, urllib.error.HTTPError):
        return f"The webhook answered HTTP {exc.code}."
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach the webhook: {exc.reason}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "The mail server rejected the user name or password."
    if isinstance(exc, smtplib.SMTPException):
        return f"The mail server refused the message: {exc}"
    if isinstance(exc, (OSError, socket.timeout)):
        return f"Could not reach the server: {exc}"
    return str(exc) or type(exc).__name__


def send_webhook(url: str, text: str) -> None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    # Slack, Mattermost, and Google Chat read "text"; Google Chat refuses unknown fields, so send only that.
    body = {"content": text[:2000]} if host in DISCORD_HOSTS else {"text": text}
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "CryoPilot"},
        method="POST",
    )
    context = _tls() if url.lower().startswith("https:") else None
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS, context=context) as response:
        response.read()


def send_email(email: dict[str, Any], password: str, subject: str, text: str) -> None:
    security = email.get("security") or "starttls"
    host = email["host"]
    port = int(email.get("port") or DEFAULT_SMTP_PORTS[security])
    username = str(email.get("username") or "").strip()
    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = ", ".join(_addresses(email.get("to")))
    message["From"] = str(email.get("from") or "").strip() or (username if "@" in username else f"CryoPilot <cryopilot@{socket.getfqdn()}>")
    message.set_content(text)
    if security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT_SECONDS, context=_tls())
    else:
        client = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT_SECONDS)
    with client:
        if security == "starttls":
            client.starttls(context=_tls())
        if username:
            client.login(username, password)
        client.send_message(message)


def _addresses(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\s]+", str(value or "")) if part.strip()]


# ------------------------------------------------------------------ settings
def enabled_events(settings: dict[str, Any]) -> dict[str, bool]:
    saved = settings.get("events") or {}
    return {event: saved.get(event, True) is not False for event in EVENTS}


def email_ready(settings: dict[str, Any]) -> bool:
    email = settings.get("email") or {}
    return bool(email.get("host") and _addresses(email.get("to")))


def view(settings: dict[str, Any], secret: Callable[[str], str], source: Callable[[str], str | None]) -> dict[str, Any]:
    """Settings for the browser. Neither the webhook address nor the password leaves the server."""

    url = secret(WEBHOOK_SECRET)
    email = settings.get("email") or {}
    security = email.get("security") or "starttls"
    return {
        "webhook": {
            "configured": bool(url),
            "host": urllib.parse.urlsplit(url).hostname if url else None,
            "source": source(WEBHOOK_SECRET),
        },
        "email": {
            "configured": email_ready(settings),
            "to": email.get("to") or "",
            "from": email.get("from") or "",
            "host": email.get("host") or "",
            "port": email.get("port") or DEFAULT_SMTP_PORTS[security],
            "security": security,
            "username": email.get("username") or "",
            "password_set": bool(secret(SMTP_SECRET)),
        },
        "events": enabled_events(settings),
        "include_message": bool(settings.get("include_message")),
        "app_url": settings.get("app_url") or "",
        "last": settings.get("last"),
    }


def apply_changes(settings: dict[str, Any], body: dict[str, Any], set_secret: Callable[[str, str | None], None]) -> dict[str, Any]:
    """The settings after a change from Settings; raises AlertSettingsError for a bad value."""

    saved = dict(settings)
    if body.get("clear_webhook"):
        set_secret(WEBHOOK_SECRET, None)
    url = str(body.get("webhook_url") or "").strip()
    if url:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise AlertSettingsError("The webhook address must start with https:// or http://.")
        set_secret(WEBHOOK_SECRET, url)
    if isinstance(body.get("events"), dict):
        events = dict(saved.get("events") or {})
        for event, value in body["events"].items():
            if event not in EVENTS:
                raise AlertSettingsError(f"Unknown alert: {event}")
            events[event] = bool(value)
        saved["events"] = events
    if "include_message" in body:
        saved["include_message"] = bool(body["include_message"])
    if "app_url" in body:
        saved["app_url"] = str(body["app_url"] or "").strip()
    email_changes = body.get("email")
    if isinstance(email_changes, dict):
        email = dict(saved.get("email") or {})
        for key in ("host", "username", "from", "to"):
            if key in email_changes:
                email[key] = str(email_changes[key] or "").strip()
        if "security" in email_changes:
            if email_changes["security"] not in SECURITY_OPTIONS:
                raise AlertSettingsError("Choose how to connect to the mail server.")
            email["security"] = email_changes["security"]
        if "port" in email_changes:
            try:
                port = int(email_changes["port"])
            except (TypeError, ValueError):
                raise AlertSettingsError("The mail server port must be a whole number.")
            if not 1 <= port <= 65535:
                raise AlertSettingsError("The mail server port must be between 1 and 65535.")
            email["port"] = port
        for address in _addresses(email.get("to")):
            if "@" not in parseaddr(address)[1]:
                raise AlertSettingsError(f"Not an email address: {address}")
        if email_changes.get("clear"):
            email = {}
            set_secret(SMTP_SECRET, None)
        if email_changes.get("password"):
            set_secret(SMTP_SECRET, str(email_changes["password"]))
        if email_changes.get("clear_password"):
            set_secret(SMTP_SECRET, None)
        saved["email"] = email
    return saved


# ------------------------------------------------------------------- sending
class AlertSender:
    """Sends alerts one at a time on a background thread, retrying a failed channel."""

    def __init__(
        self,
        *,
        settings: Callable[[], dict[str, Any]],
        secret: Callable[[str], str],
        record: Callable[[dict[str, Any]], None],
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
    ) -> None:
        self._settings = settings
        self._secret = secret
        self._record = record
        self._retry_delays = retry_delays
        self._queue: queue.Queue[tuple[Callable[[], Alert | None], Callable[[str], None] | None]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def enqueue(self, build: Callable[[], Alert | None], on_failure: Callable[[str], None] | None = None) -> None:
        """`build` runs on the sending thread and returns the alert, or None for no alert."""

        self._queue.put((build, on_failure))
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="alerts", daemon=True)
                self._thread.start()

    def wait_until_idle(self, timeout: float = 10.0) -> None:
        """For tests: returns once everything queued so far has been handled."""

        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def _channels(self, settings: dict[str, Any]) -> list[tuple[str, Callable[[str, str], None]]]:
        channels: list[tuple[str, Callable[[str, str], None]]] = []
        url = self._secret(WEBHOOK_SECRET)
        if url:
            channels.append(("webhook", lambda _subject, text: send_webhook(url, text)))
        if email_ready(settings):
            email = settings["email"]
            password = self._secret(SMTP_SECRET)
            channels.append(("email", lambda subject, text: send_email(email, password, subject, text)))
        return channels

    def _run(self) -> None:
        while True:
            build, on_failure = self._queue.get()
            try:
                self._deliver(build, on_failure)
            except Exception:  # a broken alert must never stop later ones
                log.exception("Could not send an alert")
            finally:
                self._queue.task_done()

    def _deliver(self, build: Callable[[], Alert | None], on_failure: Callable[[str], None] | None) -> None:
        settings = self._settings()
        channels = self._channels(settings)
        if not channels:
            return
        alert = build()
        if alert is None or not enabled_events(settings).get(alert.event):
            return
        subject, text = render(alert, settings)
        for name, send in channels:
            error = None
            for delay in (0, *self._retry_delays):
                if delay:
                    time.sleep(delay)
                try:
                    send(subject, text)
                    error = None
                    break
                except Exception as exc:
                    error = _plain_error(exc)
            self._note(name, alert.event, error)
            if error and on_failure is not None:
                on_failure(f"The {name} alert was not sent: {error}")

    def _note(self, channel: str, event: str, error: str | None) -> None:
        if error:
            log.warning("The %s alert was not sent: %s", channel, error)
        self._record(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "channel": channel,
                "event": event,
                "ok": error is None,
                "error": error,
            }
        )

    def test(self) -> list[dict[str, Any]]:
        """Send a test alert to every channel now, once, and say how each went."""

        settings = self._settings()
        subject = "CryoPilot: test alert"
        lines = ["CryoPilot: test alert", "Alerts from this CryoPilot server reach you here."]
        app_url = str(settings.get("app_url") or "").rstrip("/")
        if app_url:
            lines.append(app_url)
        text = "\n".join(lines)
        results = []
        for name, send in self._channels(settings):
            try:
                send(subject, text)
                results.append({"channel": name, "ok": True})
                self._note(name, "test", None)
            except Exception as exc:
                error = _plain_error(exc)
                results.append({"channel": name, "ok": False, "error": error})
                self._note(name, "test", error)
        return results
