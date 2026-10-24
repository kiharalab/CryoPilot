"""Run CryoPilot in the background, the way CryoSPARC runs.

    cryopilot start [--lan | --host HOST] [--port PORT]
    cryopilot stop
    cryopilot restart [--lan | --host HOST] [--port PORT]
    cryopilot status
    cryopilot log [-n LINES] [-f]
    cryopilot password

``start`` runs the server detached from the terminal, in its own session, so
it keeps running after you log out, until ``cryopilot stop`` or the computer
shuts down. ``--lan`` makes it answer on every network interface, so anyone on
the network can open it by the computer's name; without it the server answers
only on this computer (reach it with ``ssh -L``). The address is saved in
``APP_HOME/server.json`` and reused by ``restart`` and later starts. The
process ID is in ``APP_HOME/server.pid`` and the output in ``APP_HOME/server.log``.

A background server always asks for the shared password, set once with
``cryopilot password``; everyone who signs in shares the projects and settings.

The home folder can be shared between computers (NFS), so ``server.pid``
records which computer runs the server, and the commands act only there.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from app.server import password as lab_password
from app.server.config import APP_HOME

SETTINGS_PATH = APP_HOME / "server.json"
PID_PATH = APP_HOME / "server.pid"
LOG_PATH = APP_HOME / "server.log"
DEFAULT_PORT = int(os.environ.get("CRYOAGENT_APP_PORT", "8790"))
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALL_INTERFACES = {"0.0.0.0", "::"}
START_TIMEOUT = 120.0
STOP_TIMEOUT = 30.0
# A longer log is moved to server.log.1 when the server starts.
LOG_ROTATE_BYTES = 20 * 1024 * 1024


# ---------------------------------------------------------------- state
def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _this_machine() -> str:
    return socket.gethostname()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_cryopilot(pid: int) -> bool:
    """True when the process is a CryoPilot server, not an unrelated one that reused the ID."""

    completed = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False)
    command = completed.stdout.strip()
    return "cryopilot" in command or "serve.py" in command


def server_record() -> dict | None:
    """The running server's record, or None. Stale records from this computer are removed."""

    record = _read_json(PID_PATH)
    if not record.get("pid"):
        return None
    if record.get("machine") != _this_machine():
        # Another computer that shares this home folder; its processes are not visible here.
        return record
    if _alive(int(record["pid"])) and _is_cryopilot(int(record["pid"])):
        return record
    PID_PATH.unlink(missing_ok=True)
    return None


def _elsewhere(record: dict) -> bool:
    return record.get("machine") != _this_machine()


def _elsewhere_message(record: dict) -> str:
    return (
        f"CryoPilot was started on {record.get('machine')} (pid {record.get('pid')}), which shares this home folder.\n"
        f"Run this command on {record.get('machine')}. If that computer is off and CryoPilot is not running there,\n"
        f"delete {PID_PATH} and try again."
    )


# ------------------------------------------------------------- addresses
def addresses(host: str, port: int) -> list[str]:
    """The links people can open, best first."""

    if host in ALL_INTERFACES:
        names = [socket.getfqdn(), _this_machine()]
        try:
            # No packet is sent; this only asks which interface would carry LAN traffic.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("10.255.255.255", 1))
                names.append(probe.getsockname()[0])
        except OSError:
            pass
    else:
        names = [host]
    links = []
    for name in names:
        if not name or name in links or name.startswith("127.") or name == "localhost":
            continue
        links.append(name)
    if not links:
        links = ["localhost"]
    return [f"http://{f'[{name}]' if ':' in name else name}:{port}" for name in links]


def _answers(host: str, port: int) -> bool:
    target = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    url = f"http://{f'[{target}]' if ':' in target else target}:{port}/api/auth/status"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


# -------------------------------------------------------------- commands
def _choose_address(args: argparse.Namespace) -> tuple[str, int]:
    settings = _read_json(SETTINGS_PATH)
    if args.lan:
        host = "0.0.0.0"
    else:
        host = args.host or settings.get("host") or "127.0.0.1"
    port = args.port or int(settings.get("port") or DEFAULT_PORT)
    return host, port


def _server_command(host: str, port: int) -> list[str]:
    from app import serve

    options = ["--host", host, "--port", str(port), "--no-open"]
    if serve.PACKAGED:
        # sys.executable is the unpacked program (packaging/cryopilot.py sets it).
        return [sys.executable, *options]
    return [sys.executable, str(serve.APP_DIR / "serve.py"), *options, "--no-build"]


def _ask_password() -> str | None:
    if not sys.stdin.isatty():
        # For scripts: one line on standard input.
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("New CryoPilot password: ")
    if len(first) < lab_password.MIN_LENGTH:
        print(f"Use at least {lab_password.MIN_LENGTH} characters.", file=sys.stderr)
        return None
    if getpass.getpass("Type it again: ") != first:
        print("The passwords do not match.", file=sys.stderr)
        return None
    return first


def command_password(args: argparse.Namespace) -> int:
    replacing = lab_password.is_set()
    value = _ask_password()
    if value is None:
        return 1
    try:
        lab_password.save(value)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Saved the CryoPilot password." + (" Browsers signed in with the old one are signed out." if replacing else ""))
    record = server_record()
    if record is not None:
        print("Restart CryoPilot to use it:  cryopilot restart")
    return 0


def command_start(args: argparse.Namespace) -> int:
    record = server_record()
    if record is not None:
        if _elsewhere(record):
            print(_elsewhere_message(record), file=sys.stderr)
            return 1
        print("CryoPilot is already running.")
        return command_status(args)

    if not lab_password.is_set():
        if not sys.stdin.isatty():
            print("Set the CryoPilot password first:  cryopilot password", file=sys.stderr)
            return 2
        print("Everyone who opens CryoPilot signs in with one shared password. Choose it now;")
        print("you can change it later with:  cryopilot password")
        if command_password(args) != 0:
            return 1

    from app import serve

    host, port = _choose_address(args)
    if not serve.PACKAGED:
        serve.check_python_dependencies()
        serve.build_frontend()
    if not serve.port_is_free(host, port):
        print(f"Port {port} is already in use. Stop the program that uses it, or pass --port.", file=sys.stderr)
        return 1
    _write_json(SETTINGS_PATH, {"host": host, "port": port})

    APP_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    if LOG_PATH.is_file() and LOG_PATH.stat().st_size > LOG_ROTATE_BYTES:
        os.replace(LOG_PATH, LOG_PATH.with_name(LOG_PATH.name + ".1"))
    log = open(LOG_PATH, "a", encoding="utf-8")
    os.chmod(LOG_PATH, 0o600)
    started = datetime.now().astimezone()
    log.write(f"\n==== CryoPilot started {started:%Y-%m-%d %H:%M:%S %Z} on {host}:{port}\n")
    log.flush()
    environment = dict(os.environ)
    environment.pop("CRYOPILOT_TOKEN", None)
    try:
        process = subprocess.Popen(
            _server_command(host, port),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            cwd=str(Path.home()),
            # Its own session: no terminal, so logging out does not stop it.
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()
    _write_json(
        PID_PATH,
        {"pid": process.pid, "machine": _this_machine(), "host": host, "port": port, "started": started.isoformat(timespec="seconds")},
    )

    print("Starting CryoPilot…", flush=True)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            PID_PATH.unlink(missing_ok=True)
            print(f"CryoPilot stopped while starting (exit code {process.returncode}). The end of {LOG_PATH}:\n", file=sys.stderr)
            print(_tail(LOG_PATH, 20), file=sys.stderr)
            return 1
        if _answers(host, port):
            return command_status(args)
        time.sleep(0.5)
    print(f"CryoPilot did not answer within {START_TIMEOUT:.0f} s; it may still be starting. See:  cryopilot log", file=sys.stderr)
    return 1


def command_stop(args: argparse.Namespace) -> int:
    record = server_record()
    if record is None:
        print("CryoPilot is not running.")
        return 0
    if _elsewhere(record):
        print(_elsewhere_message(record), file=sys.stderr)
        return 1
    pid = int(record["pid"])
    print(f"Stopping CryoPilot (pid {pid}). Agent runs in progress stop too…", flush=True)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.25)
    if _alive(pid):
        print(f"CryoPilot did not stop within {STOP_TIMEOUT:.0f} s; ending it.", file=sys.stderr)
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
    PID_PATH.unlink(missing_ok=True)
    print("CryoPilot stopped.")
    return 0


def command_restart(args: argparse.Namespace) -> int:
    record = server_record()
    if record is not None and not _elsewhere(record):
        # Keep the running server's address unless the command names another.
        if not args.lan and not args.host:
            args.host = record.get("host")
        if not args.port:
            args.port = record.get("port")
    stopped = command_stop(args)
    if stopped != 0:
        return stopped
    return command_start(args)


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def command_status(args: argparse.Namespace) -> int:
    record = server_record()
    if record is None:
        print("CryoPilot is not running. Start it with:  cryopilot start")
        return 3
    if _elsewhere(record):
        print(f"CryoPilot was started on {record.get('machine')} (pid {record.get('pid')}); run this command there to check it.")
        return 0
    host, port = record["host"], int(record["port"])
    started = datetime.fromisoformat(record["started"])
    uptime = _duration((datetime.now().astimezone() - started).total_seconds())
    links = addresses(host, port)
    print(f"CryoPilot is running (pid {record['pid']}, started {started:%Y-%m-%d %H:%M}, up {uptime}).\n")
    print(f"    Open:  {links[0]}")
    for link in links[1:]:
        print(f"      or:  {link}")
    if host in LOCAL_HOSTS:
        print(f"\n    It answers only on this computer. From another one, forward the port first:")
        print(f"    ssh -L {port}:localhost:{port} <user>@{_this_machine()}")
    print(f"\n    Sign in with the CryoPilot password. Log: {LOG_PATH}")
    return 0


def _tail(path: Path, lines: int) -> str:
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 256 * 1024))
            text = stream.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def command_log(args: argparse.Namespace) -> int:
    if not LOG_PATH.is_file():
        print(f"There is no log yet ({LOG_PATH}).")
        return 0
    print(_tail(LOG_PATH, args.lines), flush=True)
    if not args.follow:
        return 0
    with open(LOG_PATH, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = stream.read()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


COMMANDS = {
    "start": (command_start, "Start CryoPilot in the background."),
    "stop": (command_stop, "Stop the background CryoPilot."),
    "restart": (command_restart, "Stop and start it again."),
    "status": (command_status, "Show whether it is running and where to open it."),
    "log": (command_log, "Show its log."),
    "password": (command_password, "Set the password everyone signs in with."),
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="cryopilot", description="Run CryoPilot in the background.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, (_, summary) in COMMANDS.items():
        sub = commands.add_parser(name, help=summary, description=summary)
        if name in {"start", "restart"}:
            where = sub.add_mutually_exclusive_group()
            where.add_argument("--lan", action="store_true", help="Answer on every network interface, so others on the network can open it.")
            where.add_argument("--host", help="Bind address (default: the saved one, else localhost only).")
            sub.add_argument("--port", type=int, help=f"Port (default: the saved one, else {DEFAULT_PORT}).")
        if name == "log":
            sub.add_argument("-n", "--lines", type=int, default=50, help="How many lines to show (default 50).")
            sub.add_argument("-f", "--follow", action="store_true", help="Keep showing new lines until Ctrl-C.")
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command][0](args)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
