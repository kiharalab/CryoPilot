"""Project files: browsing, previews, uploads, and server path completion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from . import files as file_roots, volumes
from .state import api_error, require_project, store

router = APIRouter()

# Opening a raw file in a tab must not run its scripts with the app's cookie.
# HTML, SVG, and every XML type can carry script, so anything not known to be
# inert is served sandboxed. Embedding a file in <img> or fetching it is
# unaffected.
INERT_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "application/octet-stream",
    "text/plain", "text/csv",
}
SANDBOX_POLICY = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
CONFLICT_MODES = {"replace", "rename"}


def _inert(media_type: str) -> bool:
    return media_type in INERT_TYPES or media_type.startswith("chemical/")


def _project_dirs(project_id: str) -> dict[str, Any]:
    project = require_project(project_id)
    file_roots.ensure_project_dirs(project["slug"])
    return project


def _resolve(project: dict[str, Any], root: str, path: str, *, follow_symlinks: bool = True) -> Path:
    try:
        return file_roots.resolve(project["slug"], root, path, follow_symlinks=follow_symlinks)
    except ValueError as exc:
        raise api_error(400, str(exc))


def _existing_file(project: dict[str, Any], root: str, path: str) -> Path:
    target = _resolve(project, root, path)
    if not target.is_file():
        raise api_error(404, "File not found")
    return target


def _require_writable(root: str) -> None:
    if file_roots.is_writable_root(root):
        return
    if root.startswith(file_roots.CRYOSPARC_ROOT_PREFIX):
        raise api_error(403, "CryoSPARC project folders are read-only")
    raise api_error(400, f"Unknown file root: {root}")


@router.get("/api/projects/{project_id}/files")
def list_files(project_id: str, root: str | None = None) -> dict[str, Any]:
    project = _project_dirs(project_id)
    if root and root not in file_roots.ROOT_KEYS:
        raise api_error(400, f"Unknown file root: {root}")
    roots = [root] if root else list(file_roots.ROOT_KEYS)
    entries = []
    for key in roots:
        entries.extend(file_roots.list_tree(project["slug"], key))
    return {"files": entries, "roots": {key: str(file_roots.root_path(project["slug"], key)) for key in file_roots.ROOT_KEYS}}


@router.get("/api/projects/{project_id}/files/roots")
def file_root_list(project_id: str) -> list[dict[str, Any]]:
    project = _project_dirs(project_id)
    return file_roots.project_roots(project, store.list_sessions(project["id"], "reconstruction"))


@router.get("/api/projects/{project_id}/files/tree")
def file_tree(project_id: str, root: str, path: str = "") -> dict[str, Any]:
    project = _project_dirs(project_id)
    try:
        return file_roots.list_directory(project["slug"], root, path)
    except FileNotFoundError as exc:
        raise api_error(404, str(exc))
    except PermissionError:
        raise api_error(403, "You do not have permission to read this folder")
    except ValueError as exc:
        raise api_error(400, str(exc))


@router.get("/api/projects/{project_id}/files/text")
def file_text(project_id: str, root: str, path: str) -> dict[str, Any]:
    project = _project_dirs(project_id)
    target = _existing_file(project, root, path)
    try:
        return file_roots.read_text(target)
    except PermissionError:
        raise api_error(403, "You do not have permission to read this file")


@router.get("/api/projects/{project_id}/files/table")
def file_table(project_id: str, root: str, path: str) -> dict[str, Any]:
    project = _project_dirs(project_id)
    target = _existing_file(project, root, path)
    try:
        return file_roots.read_table(target)
    except PermissionError:
        raise api_error(403, "You do not have permission to read this file")
    except ValueError as exc:
        raise api_error(400, str(exc))


@router.get("/api/projects/{project_id}/files/raw")
def raw_file(project_id: str, root: str, path: str, download: bool = False, preview: bool = False) -> FileResponse:
    project = _project_dirs(project_id)
    target = _existing_file(project, root, path)
    media_type = file_roots.guess_mime(target)
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
    if not _inert(media_type):
        headers["Content-Security-Policy"] = SANDBOX_POLICY
    served = target
    # A binned copy of a large map for the viewer, as for CryoSPARC maps.
    if preview and not download and file_roots.file_category(target.name) == "volume" and not target.name.lower().endswith(".gz"):
        try:
            served, stats = volumes.preview(target)
        except (volumes.VolumeError, OSError, ValueError) as exc:
            raise api_error(422, f"Could not read {target.name}: {exc}")
        headers["X-Map-Stats"] = volumes.stats_header(stats)
    return FileResponse(
        str(served),
        media_type=media_type,
        filename=target.name,
        content_disposition_type="attachment" if download else "inline",
        headers=headers,
    )


@router.post("/api/projects/{project_id}/files/upload")
async def upload_file(
    project_id: str,
    root: str = Form("files"),
    subdir: str = Form(""),
    on_conflict: str = Form("replace"),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    project = _project_dirs(project_id)
    _require_writable(root)
    if on_conflict not in CONFLICT_MODES:
        raise api_error(400, "on_conflict must be 'replace' or 'rename'")
    name = Path(file.filename or "upload").name
    if name in {"", ".", ".."} or name.startswith("."):
        raise api_error(400, "Choose a file with a visible name")
    folder = subdir.strip("/")
    target = _resolve(project, root, f"{folder}/{name}" if folder else name)
    if on_conflict == "rename":
        # Keep what is there (an earlier session may use it): name the upload <stem>-2<suffix> and so on.
        index = 2
        while target.exists():
            if index >= 1000:
                raise api_error(409, f"Too many files named {name} in that folder")
            numbered = file_roots.numbered_name(name, index)
            target = _resolve(project, root, f"{folder}/{numbered}" if folder else numbered)
            index += 1
    if target.is_dir():
        raise api_error(409, f"A folder named {name} already exists there")
    await run_in_threadpool(file_roots.save_upload, file.file, target)
    return {
        "root": root,
        "path": file_roots.relative_path(project["slug"], root, target),
        "name": target.name,
        "size": target.stat().st_size,
        "category": file_roots.file_category(target.name),
    }


@router.delete("/api/projects/{project_id}/files")
def delete_file(project_id: str, root: str, path: str) -> dict[str, Any]:
    project = _project_dirs(project_id)
    _require_writable(root)
    try:
        file_roots.delete_path(project["slug"], root, path)
    except FileNotFoundError as exc:
        raise api_error(404, str(exc))
    except PermissionError:
        raise api_error(403, "You do not have permission to delete this")
    except (ValueError, OSError) as exc:
        raise api_error(400, str(exc))
    return {"ok": True}


# --------------------------------------------------- server path completion
@router.get("/api/fs/list")
def server_path_list(path: str = "", dirs_only: bool = False) -> dict[str, Any]:
    try:
        return file_roots.list_server_path(path, dirs_only=dirs_only)
    except ValueError as exc:
        raise api_error(400, str(exc))
