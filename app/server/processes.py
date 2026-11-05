"""Supervises the agent driver subprocesses.

One driver process per active session. Commands go in through stdin as JSON
lines; events come back on stdout as JSON lines; stderr is the console log.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import (
    DRIVERS_DIR,
    HARNESS_SESSION_DB,
    MODELING_MEMORY_DB,
    MODELING_PYTHON,
    MODELING_SANDBOX_ROOT,
    RECONSTRUCTION_PYTHON,
)
from .credentials import ENVIRONMENT_NAMES

EventCallback = Callable[[str, dict[str, Any]], None]
LogCallback = Callable[[str, str, str], None]

# Accounts and keys the app hands to agents through state.driver_env(). They
# are dropped from the inherited environment first, so a value cleared in
# Settings stays cleared and each agent gets only its own accounts.
MANAGED_ENV = (
    *ENVIRONMENT_NAMES.values(),
    "CRYOSPARC_BASE_URL",
    "CRYOSPARC_EMAIL",
    "CRYOZETA_BASE_URL",
    "CRYOZETA_USERNAME",
)


class AgentProcess:
    def __init__(
        self,
        session_id: str,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        on_event: EventCallback,
        on_log: LogCallback,
    ) -> None:
        self.session_id = session_id
        self.command = command
        self.state = "starting"
        self.detail = ""
        self.started_at = time.time()
        self.returncode: int | None = None
        self._on_event = on_event
        self._on_log = on_log
        self._stdin_lock = threading.Lock()
        self.process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # Its own session, so a Ctrl-C or hangup in the server's terminal
            # reaches only the server, which then stops the driver in order.
            start_new_session=True,
        )
        threading.Thread(target=self._pump_stdout, name=f"stdout-{session_id}", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name=f"stderr-{session_id}", daemon=True).start()

    # ---------------------------------------------------------------- pumping
    def _pump_stdout(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                self._on_log(self.session_id, "stdout", raw)
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "status":
                self.state = str(event.get("state") or "")
                self.detail = str(event.get("detail") or "")
            self._on_event(self.session_id, event)
        self.returncode = self.process.wait()
        # The process is gone; release its pipes now rather than when the entry is replaced.
        with self._stdin_lock:
            for pipe in (self.process.stdin, self.process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
        if self.state not in {"stopped", "error"}:
            self.state = "stopped"
            self.detail = f"Driver exited with code {self.returncode}"
            self._on_event(
                self.session_id,
                {"event": "status", "state": "stopped", "detail": self.detail, "returncode": self.returncode},
            )

    def _pump_stderr(self) -> None:
        assert self.process.stderr is not None
        for raw in self.process.stderr:
            line = raw.rstrip("\n")
            if line.strip():
                self._on_log(self.session_id, "stderr", line)
        try:
            self.process.stderr.close()
        except OSError:
            pass

    # --------------------------------------------------------------- commands
    def send(self, command: dict[str, Any]) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("The agent process is not running")
        with self._stdin_lock:
            self.process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def interrupt(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)

    def shutdown(self, timeout: float = 10.0) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.send({"op": "shutdown"})
        except Exception:
            pass
        try:
            self.process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        self.interrupt()
        try:
            self.process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        self.process.kill()
        self.process.wait(timeout=5)

    @property
    def alive(self) -> bool:
        return self.process.poll() is None


class ProcessManager:
    def __init__(self, *, on_event: EventCallback, on_log: LogCallback) -> None:
        self._processes: dict[str, AgentProcess] = {}
        self._lock = threading.Lock()
        self._on_event = on_event
        self._on_log = on_log

    def get(self, session_id: str) -> AgentProcess | None:
        with self._lock:
            process = self._processes.get(session_id)
        if process is not None and not process.alive and process.returncode is not None:
            return process
        return process

    def is_alive(self, session_id: str) -> bool:
        process = self.get(session_id)
        return bool(process and process.alive)

    def running(self) -> int:
        with self._lock:
            return sum(process.alive for process in self._processes.values())

    def _register(self, process: AgentProcess) -> AgentProcess:
        with self._lock:
            self._processes[process.session_id] = process
        return process

    def _base_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        # The web app's access token is not for the agents.
        env.pop("CRYOPILOT_TOKEN", None)
        for name in MANAGED_ENV:
            env.pop(name, None)
        env["PYTHONUNBUFFERED"] = "1"
        for key, value in (extra or {}).items():
            if value:
                env[key] = value
        return env

    def start_reconstruction(
        self,
        session_id: str,
        *,
        cwd: Path,
        manifest_path: Path | None = None,
        config_path: Path | None = None,
        resume_harness_id: str | None = None,
        env: dict[str, str] | None = None,
        session_db: Path = HARNESS_SESSION_DB,
    ) -> AgentProcess:
        if self.is_alive(session_id):
            raise RuntimeError("This session already has a running agent process")
        command = [RECONSTRUCTION_PYTHON, str(DRIVERS_DIR / "reconstruction_driver.py"), "--session-db", str(session_db)]
        if resume_harness_id:
            command += ["--resume", resume_harness_id]
        else:
            if manifest_path is None:
                raise ValueError("A manifest is required for a new reconstruction session")
            command += ["--new", str(manifest_path)]
            if config_path is not None:
                command += ["--config", str(config_path)]
        cwd.mkdir(parents=True, exist_ok=True)
        process = AgentProcess(
            session_id,
            command,
            cwd=cwd,
            env=self._base_env(env),
            on_event=self._on_event,
            on_log=self._on_log,
        )
        return self._register(process)

    def start_modeling(
        self,
        session_id: str,
        *,
        workspace_relative: str,
        model: str,
        effort: str,
        max_turns: int = 500,
        env: dict[str, str] | None = None,
        memory_db: Path = MODELING_MEMORY_DB,
    ) -> AgentProcess:
        if self.is_alive(session_id):
            raise RuntimeError("This session already has a running agent process")
        command = [
            MODELING_PYTHON,
            str(DRIVERS_DIR / "modeling_driver.py"),
            "--session-id",
            session_id,
            "--memory-db",
            str(memory_db),
            "--workspace",
            workspace_relative,
            "--model",
            model,
            "--effort",
            effort,
            "--max-turns",
            str(max_turns),
        ]
        process = AgentProcess(
            session_id,
            command,
            cwd=MODELING_SANDBOX_ROOT,
            env=self._base_env({"MODELING_AGENT_SANDBOX": str(MODELING_SANDBOX_ROOT), **(env or {})}),
            on_event=self._on_event,
            on_log=self._on_log,
        )
        return self._register(process)

    def send_message(self, session_id: str, content: str) -> None:
        process = self.get(session_id)
        if process is None or not process.alive:
            raise RuntimeError("The agent is not running for this session")
        process.send({"op": "message", "content": content})

    def interrupt(self, session_id: str) -> None:
        process = self.get(session_id)
        if process is not None:
            process.interrupt()

    def stop(self, session_id: str) -> None:
        process = self.get(session_id)
        if process is not None:
            process.shutdown()
            with self._lock:
                self._processes.pop(session_id, None)

    def stop_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
            self._processes.clear()

        # In parallel, so stopping the server takes as long as the slowest
        # driver, not the sum of them.
        def stop(process: AgentProcess) -> None:
            try:
                process.shutdown(timeout=5)
            except Exception:
                pass

        threads = [threading.Thread(target=stop, args=(process,), daemon=True) for process in processes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def status(self, session_id: str) -> dict[str, Any]:
        process = self.get(session_id)
        if process is None:
            return {"running": False, "state": "stopped", "detail": ""}
        return {
            "running": process.alive,
            "state": process.state if process.alive else "stopped",
            "detail": process.detail,
            "pid": process.process.pid,
            "started_at": process.started_at,
        }
