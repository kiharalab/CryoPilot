"""Secrets the agents need: the OpenAI and Anthropic API keys and the CryoSPARC and CryoZeta passwords.

They live in this process's memory. When the user turns on "Remember on this
computer", they are also written to ``APP_HOME/credentials.json``, readable
only by the user (mode 0600), and read back when the server starts. URLs,
emails, and user names are ordinary settings and live in the app database.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .config import APP_HOME

CREDENTIALS_PATH = APP_HOME / "credentials.json"
SECRET_KEYS = ("openai_api_key", "anthropic_api_key", "cryosparc_password", "cryozeta_password")
ENVIRONMENT_NAMES = {
    "openai_api_key": "OPENAI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "cryosparc_password": "CRYOSPARC_PASSWORD",
    "cryozeta_password": "CRYOZETA_PASSWORD",
}


class Credentials:
    def __init__(self, path: Path = CREDENTIALS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._values: dict[str, str] = {}
        self._remember = False
        for key, name in ENVIRONMENT_NAMES.items():
            value = os.environ.get(name, "").strip()
            if value:
                self._values[key] = value
        self._environment_keys = set(self._values)
        self._load()

    # ---------------------------------------------------------------- access
    def get(self, key: str) -> str:
        with self._lock:
            return self._values.get(key, "")

    def is_set(self, key: str) -> bool:
        return bool(self.get(key))

    def source(self, key: str) -> str | None:
        """Where a secret came from: "environment", "app", or None if unset."""

        with self._lock:
            if not self._values.get(key):
                return None
            return "environment" if key in self._environment_keys else "app"

    @property
    def remember(self) -> bool:
        with self._lock:
            return self._remember

    # --------------------------------------------------------------- changes
    def set(self, key: str, value: str | None) -> None:
        if key not in SECRET_KEYS:
            raise KeyError(key)
        value = (value or "").strip()
        with self._lock:
            if value:
                self._values[key] = value
            else:
                self._values.pop(key, None)
            self._environment_keys.discard(key)
            self._save_locked()

    def set_remember(self, remember: bool) -> None:
        with self._lock:
            self._remember = bool(remember)
            self._save_locked()

    # ----------------------------------------------------------- persistence
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self._remember = True
        for key in SECRET_KEYS:
            value = data.get(key)
            # A secret exported in the environment wins over a remembered one.
            if isinstance(value, str) and value.strip() and key not in self._environment_keys:
                self._values[key] = value.strip()

    def _save_locked(self) -> None:
        if not self._remember:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        payload: dict[str, Any] = {
            key: self._values[key]
            for key in SECRET_KEYS
            if self._values.get(key) and key not in self._environment_keys
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkstemp makes a new file with a random name and mode 0600 (O_EXCL),
        # so a file or symlink someone placed in the folder is never written through.
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".credentials-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
