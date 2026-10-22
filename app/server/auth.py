"""Access control for the web app.

CryoPilot runs on the CryoSPARC machine, which is often shared, and the app
can start agents and open a terminal. Like Jupyter, the server therefore
accepts requests only from browsers that know a token. ``serve.py`` creates
the token at startup (or takes ``CRYOPILOT_TOKEN``) and prints a link with
``?token=...``. The browser it opens gets a one-time ``?launch=`` code
instead, so the token never appears on a command line. The server swaps
either one for an HttpOnly cookie that holds an HMAC of the token, not the
token itself. ``CRYOPILOT_AUTH=0`` turns the token check off on a
single-user computer.

In both modes the server answers only requests addressed to this computer
(the Host header, against DNS rebinding), and it refuses state-changing
requests and WebSockets that another site's page sent, including pages on
other localhost ports.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

COOKIE_PREFIX = "cryopilot_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
LAUNCH_CODE_LIFETIME = 10 * 60
# After a wrong token, no token is checked for this many seconds.
FAILURE_PAUSE = 1.0
# WebSocket close codes the browser sees: no valid sign-in, or a refused host or origin.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403

AUTH_ENABLED = os.environ.get("CRYOPILOT_AUTH", "1").strip().lower() not in {"0", "false", "no", "off"}
TOKEN = os.environ.get("CRYOPILOT_TOKEN", "").strip() or secrets.token_urlsafe(24)
# serve.py hands these to the server process. They leave the environment
# here, so agents and terminal shells do not inherit them.
LAUNCH_CODE = os.environ.pop("CRYOPILOT_LAUNCH_CODE", "").strip()
LAUNCH_DEADLINE = time.monotonic() + LAUNCH_CODE_LIFETIME
COOKIE_SECRET = os.environ.pop("CRYOPILOT_COOKIE_SECRET", "").strip() or secrets.token_urlsafe(32)

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ALL_INTERFACES = {"0.0.0.0", "::"}
BIND_HOST = os.environ.get("CRYOPILOT_BIND_HOST", "127.0.0.1").strip().lower().strip("[]")
ALLOWED_HOSTS = {
    name.strip().lower().strip("[]") for name in os.environ.get("CRYOPILOT_ALLOWED_HOSTS", "").split(",") if name.strip()
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Routes that work without the token: the login endpoints themselves.
PUBLIC_API_PREFIXES = ("/api/auth/",)

router = APIRouter()

_launch_code_used = False
_paused_until = 0.0


# ---------------------------------------------------------------- tokens
def token_matches(candidate: str | None) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), TOKEN.encode("utf-8"))


def check_token(candidate: str | None) -> str:
    """"ok", "wrong", or "paused" when a wrong token arrived less than FAILURE_PAUSE ago."""

    global _paused_until
    now = time.monotonic()
    if now < _paused_until:
        return "paused"
    if token_matches(candidate):
        return "ok"
    _paused_until = now + FAILURE_PAUSE
    return "wrong"


def use_launch_code(candidate: str | None) -> bool:
    global _launch_code_used
    if not LAUNCH_CODE or _launch_code_used or not candidate or time.monotonic() > LAUNCH_DEADLINE:
        return False
    if not hmac.compare_digest(candidate.encode("utf-8"), LAUNCH_CODE.encode("utf-8")):
        return False
    _launch_code_used = True
    return True


def session_value() -> str:
    """The cookie value: an HMAC of the token under this run's secret."""

    return hmac.new(COOKIE_SECRET.encode("utf-8"), TOKEN.encode("utf-8"), hashlib.sha256).hexdigest()


def cookie_name(scope: dict[str, Any]) -> str:
    # Cookies ignore ports, so the name carries the server's port to keep two
    # CryoPilot servers on one computer apart.
    server = scope.get("server")
    return f"{COOKIE_PREFIX}_{server[1]}" if server and server[1] else COOKIE_PREFIX


def cookie_header(scope: dict[str, Any], value: str, max_age: int = COOKIE_MAX_AGE) -> str:
    return f"{cookie_name(scope)}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"


# --------------------------------------------------------------- requests
def _headers(scope: dict[str, Any]) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}


def _header_token(headers: dict[str, str]) -> str | None:
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if "x-cryopilot-token" in headers:
        return headers["x-cryopilot-token"].strip()
    return None


def _session_cookie(headers: dict[str, str], name: str) -> str | None:
    raw_cookie = headers.get("cookie")
    if not raw_cookie:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else None


def _authenticate(scope: dict[str, Any], headers: dict[str, str]) -> str:
    """"ok", "missing", "wrong", or "paused" (see check_token)."""

    cookie = _session_cookie(headers, cookie_name(scope))
    if cookie and hmac.compare_digest(cookie.encode("utf-8"), session_value().encode("utf-8")):
        return "ok"
    token = _header_token(headers)
    if token is None:
        return "missing"
    return check_token(token)


def request_is_authenticated(request: Request) -> bool:
    if not AUTH_ENABLED:
        return True
    return _authenticate(request.scope, _headers(request.scope)) == "ok"


def host_allowed(headers: dict[str, str]) -> bool:
    """True when the request names this computer, so a DNS-rebinding page cannot reach the app."""

    if BIND_HOST in ALL_INTERFACES:
        return True
    try:
        hostname = urlsplit("//" + headers.get("host", "")).hostname
    except ValueError:
        return False
    return bool(hostname) and hostname in LOCAL_HOSTS | ALLOWED_HOSTS | {BIND_HOST}


def _same_origin(headers: dict[str, str]) -> bool:
    """False for a request another site's page sent (Origin or Sec-Fetch-Site says so)."""

    origin = headers.get("origin")
    if origin is not None and origin.split("://", 1)[-1].rstrip("/").lower() != headers.get("host", "").lower():
        return False
    return headers.get("sec-fetch-site", "same-origin") in {"same-origin", "none"}


async def _send_json(send, status: int, payload: dict[str, Any], extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *(extra_headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _close_websocket(receive, send, code: int) -> None:
    # A close before the handshake reaches the browser only as a failed
    # connection (1006), so accept first and then close with the code.
    if (await receive()).get("type") == "websocket.connect":
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": code})


async def _refuse(kind: str, receive, send, message: str) -> None:
    """Turn away a request for another host or from another origin (403, not a sign-out)."""

    if kind == "websocket":
        await _close_websocket(receive, send, CLOSE_FORBIDDEN)
    else:
        await _send_json(send, 403, {"error": message})


class AccessTokenMiddleware:
    """Plain ASGI middleware so the checks also cover WebSocket upgrades."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        kind = scope.get("type")
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        headers = _headers(scope)

        if not host_allowed(headers):
            await _refuse(
                kind,
                receive,
                send,
                "CryoPilot answers only at localhost or the host name it was started with. "
                "Add other host names to CRYOPILOT_ALLOWED_HOSTS.",
            )
            return
        if (kind == "websocket" or scope.get("method") not in SAFE_METHODS) and not _same_origin(headers):
            await _refuse(kind, receive, send, "CryoPilot accepts changes only from its own pages.")
            return
        if not AUTH_ENABLED:
            await self.app(scope, receive, send)
            return

        # Opening the printed link or the browser's launch link: trade the
        # token or the one-time code for the cookie and drop it from the URL.
        query = dict(parse_qsl((scope.get("query_string") or b"").decode("latin-1"), keep_blank_values=True))
        if kind == "http" and not path.startswith("/api/") and ("token" in query or "launch" in query):
            if ("launch" in query and use_launch_code(query["launch"])) or ("token" in query and check_token(query["token"]) == "ok"):
                remaining = {key: value for key, value in query.items() if key not in {"token", "launch"}}
                location = path + (f"?{urlencode(remaining)}" if remaining else "")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 302,
                        "headers": [
                            (b"location", location.encode("latin-1")),
                            (b"set-cookie", cookie_header(scope, session_value()).encode("latin-1")),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return

        if not path.startswith("/api/") or path.startswith(PUBLIC_API_PREFIXES):
            await self.app(scope, receive, send)
            return

        result = _authenticate(scope, headers)
        if result == "ok":
            await self.app(scope, receive, send)
        elif kind == "websocket":
            await _close_websocket(receive, send, CLOSE_UNAUTHORIZED)
        elif result == "paused":
            await _send_json(send, 429, {"error": "Too many wrong access tokens. Wait a moment and try again."}, [(b"retry-after", b"1")])
        else:
            await _send_json(send, 401, {"error": "Sign in with the access link printed when CryoPilot started.", "auth_required": True})


# ------------------------------------------------------------------ routes
@router.get("/api/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    return {"enabled": AUTH_ENABLED, "authenticated": request_is_authenticated(request)}


@router.post("/api/auth/login")
async def auth_login(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    candidate = str((body or {}).get("token") or "").strip()
    # Accept the whole printed link as well as the bare token.
    if "token=" in candidate:
        candidate = dict(parse_qsl(candidate.split("?", 1)[-1])).get("token", "")
    if AUTH_ENABLED:
        result = check_token(candidate)
        if result == "paused":
            return JSONResponse({"error": "Too many wrong access tokens. Wait a moment and try again."}, status_code=429)
        if result != "ok":
            return JSONResponse({"error": "That access token is not valid for this server."}, status_code=401)
    response = JSONResponse({"ok": True})
    if AUTH_ENABLED:
        response.headers["set-cookie"] = cookie_header(request.scope, session_value())
    return response


@router.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.headers["set-cookie"] = cookie_header(request.scope, "", max_age=0)
    return response
