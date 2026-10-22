"""Shells in pseudo-terminals for the app's integrated terminal.

Each terminal is the user's login shell on its own PTY. A reader thread
copies the shell's output into a scrollback buffer and to every connected
WebSocket, so a terminal keeps running (and keeps its history) when the
browser disconnects or reloads. A terminal ends when the user closes it,
when its shell exits, or when the server stops.
"""

from __future__ import annotations

import asyncio
import atexit
import codecs
import fcntl
import os
import pty
import pwd
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_TERMINALS = 12
SCROLLBACK_CHARS = 256 * 1024
READ_SIZE = 64 * 1024
# A browser that falls this far behind is disconnected; it reconnects and replays the scrollback.
SUBSCRIBER_BACKLOG_CHARS = 4 * 1024 * 1024
DEFAULT_COLS = 80
DEFAULT_ROWS = 24
MAX_DIMENSION = 1000
# Variables the shell must not inherit: the app's access token, and markers of
# the terminal the server itself was started from.
HIDDEN_ENV = {
    "CRYOPILOT_TOKEN",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "TERM_SESSION_ID",
    "ITERM_SESSION_ID",
    "ITERM_PROFILE",
    "LC_TERMINAL",
    "LC_TERMINAL_VERSION",
}


class TerminalError(Exception):
    pass


class TerminalLimitError(TerminalError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def shell_command() -> tuple[str, list[str]]:
    """The user's shell and its argv; an argv[0] starting with '-' starts a login shell."""

    candidates = [os.environ.get("SHELL") or ""]
    try:
        candidates.append(pwd.getpwuid(os.getuid()).pw_shell)
    except KeyError:
        pass
    candidates += ["/bin/bash", "/bin/sh"]
    shell = next(path for path in candidates if path and os.path.isabs(path) and os.access(path, os.X_OK))
    return shell, ["-" + os.path.basename(shell)]


def shell_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in HIDDEN_ENV}
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    if not any(env.get(key) for key in ("LC_ALL", "LC_CTYPE", "LANG")):
        env["LANG"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return env


def _clamp(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_DIMENSION, number))


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _take_controlling_terminal() -> None:
    # Runs in the child after setsid() and before exec, so the PTY becomes the
    # shell's controlling terminal: job control works and Ctrl+C reaches the
    # foreground program.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Subscriber:
    """One WebSocket's view of a terminal: output chunks queued on its event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.closed = False
        self._backlog = 0
        self._lock = threading.Lock()

    def push(self, kind: str, payload: Any) -> None:
        if self.closed:
            return
        if kind == "output":
            with self._lock:
                self._backlog += len(payload)
                overflowing = self._backlog > SUBSCRIBER_BACKLOG_CHARS
            if overflowing:
                kind, payload = "overflow", None
                self.closed = True
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (kind, payload))
        except RuntimeError:  # the connection's event loop is gone
            self.closed = True

    def taken(self, chars: int) -> None:
        with self._lock:
            self._backlog = max(0, self._backlog - chars)


class Terminal:
    def __init__(
        self,
        *,
        cwd: Path,
        title: str,
        project_id: str | None = None,
        root: str | None = None,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.cwd = str(cwd)
        self.project_id = project_id
        self.root = root
        self.created_at = _now()
        self.exited_at: str | None = None
        self.exit_code: int | None = None
        self.alive = False
        self.cols = _clamp(cols, DEFAULT_COLS)
        self.rows = _clamp(rows, DEFAULT_ROWS)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._scrollback: deque[str] = deque()
        self._scrollback_size = 0
        self._scrollback_trimmed = False
        self._subscribers: set[Subscriber] = set()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._closing = False
        self._master: int | None = None
        self.process: subprocess.Popen | None = None
        self.shell = ""
        self._reader: threading.Thread | None = None

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        executable, argv = shell_command()
        self.shell = os.path.basename(executable)
        master, slave = pty.openpty()
        try:
            _set_size(slave, self.cols, self.rows)
            self.process = subprocess.Popen(
                argv,
                executable=executable,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=self.cwd,
                env=shell_environment(),
                start_new_session=True,
                preexec_fn=_take_controlling_terminal,
                close_fds=True,
            )
        except Exception:
            os.close(master)
            raise
        finally:
            # The parent keeps only the master side; otherwise the PTY never
            # reports end-of-file when the shell exits.
            os.close(slave)
        # Non-blocking, so a write never waits inside os.write while holding the
        # lock that _finish closes the descriptor under (see write()).
        os.set_blocking(master, False)
        self._master = master
        self.alive = True
        self._reader = threading.Thread(target=self._read_loop, name=f"terminal-{self.id}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        fd = self._master
        assert fd is not None and self.process is not None
        try:
            while not self._closing:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                except (OSError, ValueError):
                    break
                if not ready:
                    if self.process.poll() is not None:
                        break  # the shell exited; a background job may still hold the PTY open
                    continue
                try:
                    chunk = os.read(fd, READ_SIZE)
                except BlockingIOError:
                    continue
                except OSError:  # EIO once the shell side closes (Linux)
                    break
                if not chunk:  # end of file (macOS)
                    break
                self._emit(self._decoder.decode(chunk))
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._emit(tail)
        finally:
            self._finish()

    def _finish(self) -> None:
        assert self.process is not None
        try:
            code = self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._signal_group(signal.SIGKILL)
            code = self.process.wait()
        if code < 0:
            code = 128 - code  # the shell convention for "killed by signal N"
        with self._lock:
            master, self._master = self._master, None
            if master is not None:
                try:
                    os.close(master)
                except OSError:
                    pass
            self.alive = False
            self.exit_code = code
            self.exited_at = _now()
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.push("exit", code)

    def _signal_group(self, signum: int) -> None:
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, signum)
        except (ProcessLookupError, PermissionError):
            pass

    def kill(self) -> None:
        """Hang up the shell (and its jobs), then force it if it does not exit."""

        if self.process is None:
            return
        if self.process.poll() is None:
            self._signal_group(signal.SIGHUP)
        if self._reader is not None:
            self._reader.join(timeout=1.5)
        if self.process.poll() is None:
            self._signal_group(signal.SIGKILL)
        self._closing = True
        if self._reader is not None:
            self._reader.join(timeout=3)

    # --------------------------------------------------------------- output
    def _emit(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._scrollback.append(text)
            self._scrollback_size += len(text)
            while self._scrollback_size > SCROLLBACK_CHARS and len(self._scrollback) > 1:
                self._scrollback_size -= len(self._scrollback.popleft())
                self._scrollback_trimmed = True
            if self._scrollback_size > SCROLLBACK_CHARS:
                only = self._scrollback.pop()[-SCROLLBACK_CHARS:]
                self._scrollback.append(only)
                self._scrollback_size = len(only)
                self._scrollback_trimmed = True
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.push("output", text)

    def _scrollback_text(self) -> str:
        text = "".join(self._scrollback)
        if self._scrollback_trimmed:
            # Start the replay on a fresh line rather than inside an escape sequence.
            newline = text.find("\n", 0, 4096)
            if newline >= 0:
                text = text[newline + 1:]
        return text

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> tuple[Subscriber, str]:
        """A new subscriber and the scrollback it should replay first, taken atomically."""

        subscriber = Subscriber(loop)
        with self._lock:
            backlog = self._scrollback_text()
            if self.alive:
                self._subscribers.add(subscriber)
        return subscriber, backlog

    def unsubscribe(self, subscriber: Subscriber) -> None:
        subscriber.closed = True
        with self._lock:
            self._subscribers.discard(subscriber)

    # ---------------------------------------------------------------- input
    def write(self, data: str) -> None:
        payload = data.encode("utf-8", errors="replace")
        with self._write_lock:
            while payload:
                fd = self._master
                if fd is None:
                    return
                # Wait for room outside the lock; a full PTY (a busy shell) must not hold up _finish.
                try:
                    _, writable, _ = select.select([], [fd], [], 0.25)
                except (OSError, ValueError):
                    return
                if not writable:
                    continue
                # _finish closes the descriptor under this lock, so the write
                # can never reach a closed (and possibly reused) descriptor.
                with self._lock:
                    if self._master is None:
                        return
                    try:
                        written = os.write(self._master, payload)
                    except BlockingIOError:
                        continue
                    except OSError:
                        return
                payload = payload[written:]

    def resize(self, cols: Any, rows: Any) -> None:
        self.cols = _clamp(cols, self.cols)
        self.rows = _clamp(rows, self.rows)
        with self._lock:
            if self._master is None:
                return
            try:
                _set_size(self._master, self.cols, self.rows)
            except OSError:
                pass

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "shell": self.shell,
            "project_id": self.project_id,
            "root": self.root,
            "alive": self.alive,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "exited_at": self.exited_at,
            "cols": self.cols,
            "rows": self.rows,
        }


class TerminalManager:
    def __init__(self) -> None:
        self._terminals: dict[str, Terminal] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            terminals = list(self._terminals.values())
        return [terminal.view() for terminal in terminals]

    def get(self, terminal_id: str) -> Terminal | None:
        with self._lock:
            return self._terminals.get(terminal_id)

    def create(
        self,
        *,
        cwd: Path,
        title: str,
        project_id: str | None = None,
        root: str | None = None,
        cols: Any = None,
        rows: Any = None,
    ) -> Terminal:
        if not cwd.is_dir():
            raise TerminalError(f"No folder at {cwd}")
        terminal = Terminal(
            cwd=cwd,
            title=title,
            project_id=project_id,
            root=root,
            cols=_clamp(cols, DEFAULT_COLS),
            rows=_clamp(rows, DEFAULT_ROWS),
        )
        with self._lock:
            self._prune_exited()
            if sum(entry.alive for entry in self._terminals.values()) >= MAX_TERMINALS:
                raise TerminalLimitError(f"Close a terminal before opening another; at most {MAX_TERMINALS} can be open.")
            terminal.start()
            self._terminals[terminal.id] = terminal
        return terminal

    def _prune_exited(self) -> None:
        # Exited terminals stay listed (so the page can show that they ended)
        # until they are closed; keep only the most recent ones.
        exited = [entry for entry in self._terminals.values() if not entry.alive]
        for entry in exited[:-MAX_TERMINALS]:
            self._terminals.pop(entry.id, None)

    def close(self, terminal_id: str) -> bool:
        with self._lock:
            terminal = self._terminals.pop(terminal_id, None)
        if terminal is None:
            return False
        terminal.kill()
        return True

    def shutdown(self) -> None:
        with self._lock:
            terminals = list(self._terminals.values())
            self._terminals.clear()
        for terminal in terminals:
            if terminal.process is not None and terminal.process.poll() is None:
                terminal._signal_group(signal.SIGHUP)
        for terminal in terminals:
            terminal.kill()


manager = TerminalManager()
atexit.register(manager.shutdown)
