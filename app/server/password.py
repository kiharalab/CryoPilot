"""The shared CryoPilot password, for a server that others reach over the network.

``cryopilot password`` stores a salted scrypt hash in ``APP_HOME/password.json``
(mode 0600). When the file exists, the server asks for this password instead
of the access token printed at startup. The secret that signs sign-in cookies
is kept next to it in ``APP_HOME/cookie_secret``, so sign-ins survive a
restart; the cookie also depends on the hash, so a new password signs every
browser out.

Standard library only: the service commands use it without starting the server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from .config import APP_HOME

PASSWORD_PATH = APP_HOME / "password.json"
COOKIE_SECRET_PATH = APP_HOME / "cookie_secret"
MIN_LENGTH = 8
# About 50 ms and 16 MB per check, which slows guessing without slowing sign-in.
SCRYPT_PARAMETERS = {"n": 2**14, "r": 8, "p": 1}


def _write_private(path: Path, text: str) -> None:
    """Write a file only this user can read, replacing any old one in one step."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _derive(password: str, salt: bytes, parameters: dict[str, int]) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=parameters["n"], r=parameters["r"], p=parameters["p"], dklen=32
    )


def is_set() -> bool:
    return PASSWORD_PATH.is_file()


def load() -> dict[str, Any] | None:
    """The stored hash, or None when no password is set."""

    try:
        record = json.loads(PASSWORD_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {PASSWORD_PATH}: {exc}. Set the password again with: cryopilot password") from exc
    if record.get("scheme") != "scrypt" or not record.get("salt") or not record.get("hash"):
        raise RuntimeError(f"{PASSWORD_PATH} is not a CryoPilot password file. Set the password again with: cryopilot password")
    return record


def save(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    record = {
        "scheme": "scrypt",
        **SCRYPT_PARAMETERS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(_derive(password, salt, SCRYPT_PARAMETERS)).decode("ascii"),
    }
    _write_private(PASSWORD_PATH, json.dumps(record, indent=2) + "\n")


def verify(record: dict[str, Any], candidate: str) -> bool:
    if not candidate:
        return False
    parameters = {key: int(record[key]) for key in SCRYPT_PARAMETERS}
    expected = base64.b64decode(record["hash"])
    return hmac.compare_digest(_derive(candidate, base64.b64decode(record["salt"]), parameters), expected)


def cookie_secret() -> str:
    """The persistent secret that signs sign-in cookies, made on first use."""

    try:
        value = COOKIE_SECRET_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if len(value) < 32:
        value = secrets.token_urlsafe(32)
        _write_private(COOKIE_SECRET_PATH, value + "\n")
    return value
