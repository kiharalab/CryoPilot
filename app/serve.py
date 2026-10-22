#!/usr/bin/env python3
"""Start the CryoPilot web app with one command.

    python app/serve.py [--port 8790] [--no-open] [--dev] [--no-auth]

Builds the frontend on first run (or when sources changed), starts the API
server on localhost, and opens the app in your browser. The app asks for an
access token, which is part of the link printed at startup; set
CRYOPILOT_TOKEN (at least 22 characters) to keep the same token across
restarts. The browser opened here gets a one-time launch code instead of the
token, so the token never appears on a command line other users can read.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# In the packaged executable the frontend is prebuilt and the dependencies are
# compiled in, so the build and dependency checks do not apply.
PACKAGED = "__compiled__" in globals()

APP_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = APP_DIR / "frontend"
DIST_DIR = FRONTEND_DIR / "dist"
REPO_ROOT = APP_DIR.parent
# A chosen CRYOPILOT_TOKEN must be about as hard to guess as a generated one.
MIN_TOKEN_LENGTH = 22


def newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for path in paths:
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    newest = max(newest, child.stat().st_mtime)
    return newest


def frontend_is_stale() -> bool:
    index = DIST_DIR / "index.html"
    if not index.exists():
        return True
    sources = [FRONTEND_DIR / "src", FRONTEND_DIR / "index.html", FRONTEND_DIR / "package.json", FRONTEND_DIR / "vite.config.js"]
    return newest_mtime(sources) > index.stat().st_mtime


def build_frontend(force: bool = False) -> None:
    if not force and not frontend_is_stale():
        return
    npm = shutil.which("npm")
    if npm is None:
        print("npm was not found. Install Node.js 18+ and re-run, or build app/frontend manually.", file=sys.stderr)
        raise SystemExit(2)
    if not (FRONTEND_DIR / "node_modules").is_dir():
        print("Installing frontend dependencies (first run only)…", flush=True)
        subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=FRONTEND_DIR, check=True)
    print("Building the frontend…", flush=True)
    subprocess.run([npm, "run", "build"], cwd=FRONTEND_DIR, check=True)


def importable(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def check_python_dependencies() -> None:
    missing = []
    for module, package in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("multipart", "python-multipart")):
        if not importable(module):
            missing.append(package)
    # Without a WebSocket library uvicorn refuses every upgrade, and the terminal never connects.
    if not importable("websockets") and not importable("wsproto"):
        missing.append("websockets")
    if missing:
        print(
            "Missing Python packages: " + ", ".join(missing) + "\n"
            f"Install them with: {sys.executable} -m pip install -r {REPO_ROOT / 'requirements.txt'} -r {APP_DIR / 'requirements.txt'}\n"
            "or run ./setup.sh again.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def port_is_free(host: str, port: int) -> bool:
    """Bind the port the way uvicorn does, to find out whether another program holds it."""

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def open_browser_when_listening(host: str, port: int, url: str, timeout: float = 60.0) -> None:
    """Open the browser once the server accepts connections, not before."""

    target = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=1):
                break
        except OSError:
            time.sleep(0.2)
    else:
        return
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the CryoPilot web app.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: localhost only).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CRYOAGENT_APP_PORT", "8790")))
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser window.")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Do not ask for an access token. Any user or program on this computer can then use the app, "
            "so use it only on a computer no one else can log in to."
        ),
    )
    def source_only(text: str) -> str:
        return argparse.SUPPRESS if PACKAGED else text

    parser.add_argument("--no-build", action="store_true", help=source_only("Skip the frontend build check."))
    parser.add_argument("--rebuild", action="store_true", help=source_only("Force a frontend rebuild."))
    parser.add_argument("--dev", action="store_true", help=source_only("Auto-reload the API server on code changes."))
    args = parser.parse_args(argv)

    if not PACKAGED:
        check_python_dependencies()
        if not args.no_build:
            build_frontend(force=args.rebuild)

    if str(APP_DIR.parent) not in sys.path:
        sys.path.insert(0, str(APP_DIR.parent))
    os.chdir(APP_DIR.parent)

    import uvicorn

    # The server process (and its reloader child) reads these from the
    # environment: the token, a one-time code for the browser opened below,
    # the secret that signs the session cookie, and the host it answers to.
    os.environ["CRYOPILOT_BIND_HOST"] = args.host
    if args.no_auth:
        os.environ["CRYOPILOT_AUTH"] = "0"
        os.environ.pop("CRYOPILOT_TOKEN", None)
    else:
        token = os.environ.get("CRYOPILOT_TOKEN", "").strip()
        if token and len(token) < MIN_TOKEN_LENGTH:
            print(
                f"CRYOPILOT_TOKEN is too short to keep other users out. Use at least {MIN_TOKEN_LENGTH} characters, "
                "for example the output of:\n"
                "    python3 -c 'import secrets; print(secrets.token_urlsafe(24))'",
                file=sys.stderr,
            )
            return 2
        os.environ["CRYOPILOT_AUTH"] = "1"
        os.environ["CRYOPILOT_TOKEN"] = token or secrets.token_urlsafe(24)
        os.environ["CRYOPILOT_LAUNCH_CODE"] = secrets.token_urlsafe(24)
        os.environ["CRYOPILOT_COOKIE_SECRET"] = os.environ.get("CRYOPILOT_COOKIE_SECRET", "").strip() or secrets.token_urlsafe(32)

    if not port_is_free(args.host, args.port):
        print(f"Port {args.port} is already in use. Stop the program that uses it, or pass --port.", file=sys.stderr)
        return 1

    url_host = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{url_host}:{args.port}"
    link = url if args.no_auth else f"{url}/?token={os.environ['CRYOPILOT_TOKEN']}"
    print(f"\nCryoPilot is running. Open this link in your browser:\n\n    {link}\n", flush=True)
    if args.host in {"127.0.0.1", "localhost"}:
        print(
            "From another computer, forward the port first, for example:\n"
            f"    ssh -L {args.port}:localhost:{args.port} <user>@<this-computer>\n",
            flush=True,
        )
    if not args.no_open:
        launch = url if args.no_auth else f"{url}/?launch={os.environ['CRYOPILOT_LAUNCH_CODE']}"
        threading.Thread(target=open_browser_when_listening, args=(args.host, args.port, launch), daemon=True).start()
    if PACKAGED:
        from app.server.main import app as asgi_app
    uvicorn.run(
        asgi_app if PACKAGED else "app.server.main:app",
        host=args.host,
        port=args.port,
        reload=args.dev and not PACKAGED,
        reload_dirs=[str(APP_DIR / "server")] if args.dev else None,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
