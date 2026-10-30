"""Thin CryoSPARC access for the web app through cryosparc-tools.

Every function takes ``credentials`` as ``(url, email, password)`` (see
``state.cryosparc_credentials``). Clients are created once per URL, email,
and password and reused; a client whose session has expired is replaced and
the call retried once.

Signing in makes network calls, so it runs outside the shared lock: callers
with the same credentials wait for one sign-in, and callers with other
credentials are not held up. A failed sign-in is remembered for a short time,
so an unreachable CryoSPARC fails requests quickly instead of making each one
wait for its own connection timeout.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")
Credentials = tuple[str, str, str]
Key = tuple[str, str, str]

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 120.0
FAILED_SIGN_IN_SECONDS = 30.0

_lock = threading.Lock()
_clients: dict[Key, Any] = {}
_sign_in_locks: dict[Key, threading.Lock] = {}
_failures: dict[Key, tuple[float, str]] = {}


class SignInError(RuntimeError):
    """A sign-in that failed a moment ago, reported again without retrying it."""


def _key(url: str, email: str, password: str) -> Key:
    # A new password gets a new client; the password itself is not kept here.
    return url.rstrip("/"), email, hashlib.sha256(password.encode("utf-8")).hexdigest()


def _connect(url: str, email: str, password: str):
    import httpx
    from cryosparc.tools import CryoSPARC

    # A host that drops packets fails within the connect timeout; reads keep the long limit.
    timeout = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    return CryoSPARC(url.rstrip("/"), email=email, password=password, timeout=timeout)


def _client(url: str, email: str, password: str):
    key = _key(url, email, password)
    with _lock:
        client = _clients.get(key)
        if client is not None:
            return client
        sign_in = _sign_in_locks.setdefault(key, threading.Lock())
    with sign_in:
        with _lock:
            client = _clients.get(key)
            if client is not None:
                return client
            failure = _failures.get(key)
            if failure is not None and time.monotonic() - failure[0] < FAILED_SIGN_IN_SECONDS:
                raise SignInError(failure[1])
        try:
            client = _connect(url, email, password)
        except Exception as exc:
            with _lock:
                _failures[key] = (time.monotonic(), str(exc) or type(exc).__name__)
            raise
        with _lock:
            # Clients signed in with an older password of this account are no longer needed.
            for cache in (_clients, _failures, _sign_in_locks):
                for stale in [other for other in cache if other[:2] == key[:2] and other != key]:
                    del cache[stale]
            _failures.pop(key, None)
            _clients[key] = client
    return client


def forget(url: str, email: str, password: str) -> None:
    key = _key(url, email, password)
    with _lock:
        _clients.pop(key, None)
        _failures.pop(key, None)


def _call(credentials: Credentials, action: Callable[[Any], T]) -> T:
    """Run ``action(client)``, reconnecting once if CryoSPARC rejects the session."""

    try:
        from cryosparc.errors import APIError
    except ImportError:  # pragma: no cover - cryosparc-tools is a server requirement
        APIError = ()  # type: ignore[assignment]
    client = _client(*credentials)
    try:
        return action(client)
    except APIError as exc:  # type: ignore[misc]
        if getattr(exc, "code", None) not in (401, 403):
            raise
    forget(*credentials)
    return action(_client(*credentials))


def test_connection(url: str, email: str, password: str) -> dict[str, Any]:
    # A test always signs in again, even within the wait after a failed sign-in.
    forget(url, email, password)
    client = _client(url, email, password)
    projects = list(client.find_projects())
    return {"ok": True, "project_count": len(projects)}


def list_projects(credentials: Credentials) -> list[dict[str, Any]]:
    """The account's projects, newest first, as ``{uid, title, created_at}``."""

    def action(client: Any) -> list[dict[str, Any]]:
        projects = []
        for project in client.find_projects():
            model = project.model
            created = getattr(model, "created_at", None)
            projects.append(
                {
                    "uid": str(model.uid),
                    "title": str(getattr(model, "title", "") or ""),
                    "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
                }
            )
        return sorted(projects, key=lambda project: int(project["uid"][1:]) if project["uid"][1:].isdigit() else 0, reverse=True)

    return _call(credentials, action)


def project_directory(credentials: Credentials, project_uid: str) -> str:
    return _call(credentials, lambda client: str(client.find_project(project_uid).dir))


def job_documents(credentials: Credentials, project_uid: str, workspace_uid: str | None) -> list[dict[str, Any]]:
    """Every job of a project (or one workspace) as plain JSON-style dicts."""

    def action(client: Any) -> list[dict[str, Any]]:
        jobs = client.find_jobs(project_uid, workspace_uid or None, order=-1)
        return [job.model.model_dump(mode="json") for job in jobs]

    return _call(credentials, action)


def job_document(credentials: Credentials, project_uid: str, job_uid: str) -> dict[str, Any]:
    return _call(credentials, lambda client: client.find_job(project_uid, job_uid).model.model_dump(mode="json"))


def dataset_field(credentials: Credentials, project_uid: str, path: str, field: str) -> Any:
    """First value of ``field`` in the ``.cs`` dataset at a project-relative ``path``."""

    def action(client: Any) -> Any:
        dataset = client.download_dataset(project_uid, path)
        return dataset[field][0]

    return _call(credentials, action)


def download_project_file(credentials: Credentials, project_uid: str, path: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    _call(credentials, lambda client: client.download_file(project_uid, path, target))
    return target


def list_assets(credentials: Credentials, project_uid: str, job_uid: str) -> list[dict[str, Any]]:
    """Files CryoSPARC stored in its database for a job (plots, class averages)."""

    def action(client: Any) -> list[dict[str, Any]]:
        assets = []
        for asset in client.list_assets(project_uid, job_uid):
            uploaded = getattr(asset, "uploadDate", None)
            assets.append(
                {
                    "id": str(getattr(asset, "id", "") or ""),
                    "filename": str(getattr(asset, "filename", "") or ""),
                    "content_type": str(getattr(asset, "contentType", "") or ""),
                    "size": int(getattr(asset, "length", 0) or 0),
                    "uploaded_at": uploaded.isoformat() if hasattr(uploaded, "isoformat") else uploaded,
                }
            )
        return assets

    return _call(credentials, action)


def download_asset(credentials: Credentials, asset_id: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    _call(credentials, lambda client: client.download_asset(asset_id, target))
    return target
