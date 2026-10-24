#!/usr/bin/env python3
"""Start the CryoPilot web app with one command.

    python app/serve.py [--port 8790] [--no-open] [--dev] [--no-auth]
    python app/serve.py start|stop|restart|status|log|password

Builds the frontend on first run (or when sources changed), starts the API
server on localhost, and opens the app in your browser. The app asks for an
access token, which is part of the link printed at startup; set
CRYOPILOT_TOKEN (at least 22 characters) to keep the same token across
restarts. The browser opened here gets a one-time launch code instead of the
token, so the token never appears on a command line other users can read.
Once a shared password is set (``password``), the app asks for it instead.

The other commands run the server in the background, like CryoSPARC; see
service.py.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
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
# The background-server commands (service.py); anything else runs the server here.
SERVICE_COMMANDS = {"start", "stop", "restart", "status", "log", "password"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
        print("npm was not found. Install Node.js 20.19+ and re-run, or build app/frontend manually.", file=sys.stderr)
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
    argv = sys.argv[1:] if argv is None else argv
    if str(APP_DIR.parent) not in sys.path:
        sys.path.insert(0, str(APP_DIR.parent))
    if argv and argv[0] in SERVICE_COMMANDS:
        from app import service

        return service.main(argv)

    parser = argparse.ArgumentParser(
        description="Run the CryoPilot web app.",
        epilog="To run it in the background instead: cryopilot start|stop|restart|status|log|password (add -h to each).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: localhost only).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CRYOPILOT_APP_PORT", "8790")))
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

    os.chdir(APP_DIR.parent)

    import uvicorn

    from app.server import password as lab_password

    # The server process (and its reloader child) reads these from the
    # environment: the token, a one-time code for the browser opened below,
    # the secret that signs the session cookie, and the host it answers to.
    os.environ["CRYOPILOT_BIND_HOST"] = args.host
    use_password = not args.no_auth and lab_password.is_set()
    if args.no_auth and args.host not in LOCAL_HOSTS:
        print(
            "--no-auth works only when CryoPilot answers on this computer alone (the default --host).\n"
            "For other computers, set a password with:  cryopilot password",
            file=sys.stderr,
        )
        return 2
    if args.no_auth:
        os.environ["CRYOPILOT_AUTH"] = "0"
        os.environ.pop("CRYOPILOT_TOKEN", None)
    elif use_password:
        # The server reads the password file itself; no token is made or printed.
        os.environ["CRYOPILOT_AUTH"] = "1"
        os.environ.pop("CRYOPILOT_TOKEN", None)
        os.environ["CRYOPILOT_LAUNCH_CODE"] = secrets.token_urlsafe(24)
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
    if args.host in {"0.0.0.0", "::"}:
        from app.service import addresses

        public_url = addresses(args.host, args.port)[0]
    else:
        public_url = url
    if args.no_auth or use_password:
        print(f"\nCryoPilot is running. Open this link in your browser:\n\n    {public_url}\n", flush=True)
        if use_password:
            print("Sign in with the CryoPilot password.\n", flush=True)
    else:
        link = f"{public_url}/?token={os.environ['CRYOPILOT_TOKEN']}"
        print(f"\nCryoPilot is running. Open this link in your browser:\n\n    {link}\n", flush=True)
    if args.host in LOCAL_HOSTS:
        print(
            "From another computer, forward the port first, for example:\n"
            f"    ssh -L {args.port}:localhost:{args.port} <user>@<this-computer>\n",
            flush=True,
        )
    if not args.no_open:
        launch = url if args.no_auth else f"{url}/?launch={os.environ['CRYOPILOT_LAUNCH_CODE']}"
        threading.Thread(target=open_browser_when_listening, args=(args.host, args.port, launch), daemon=True).start()
    options = dict(
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
        # Open session pages keep event streams open. They end on their own
        # when shutdown starts; anything still open after 5 s is closed, and
        # the shutdown hook that stops the agent processes still runs.
        timeout_graceful_shutdown=5,
    )
    if args.dev and not PACKAGED:
        uvicorn.run("app.server.main:app", reload=True, reload_dirs=[str(APP_DIR / "server")], **options)
        return 0
    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            # Event streams check this flag and end, so shutdown does not wait on them.
            from app.server.state import shutting_down

            shutting_down.set()
            super().handle_exit(sig, frame)

    if PACKAGED:
        from app.server.main import app as asgi_app
    server = Server(uvicorn.Config(asgi_app if PACKAGED else "app.server.main:app", **options))
    # A closed terminal or a dropped SSH connection sends SIGHUP: stop the same way as on Ctrl-C.
    signal.signal(signal.SIGHUP, server.handle_exit)
    try:
        server.run()
    except KeyboardInterrupt:
        # uvicorn raises Ctrl-C again after its clean shutdown; uvicorn.run() hides it the same way.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
