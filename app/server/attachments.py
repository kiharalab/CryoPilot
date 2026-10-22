"""Inputs attached to a modeling message: CryoSPARC maps and project files.

The modeling agent's tools refuse any path outside its sandbox (they resolve
symlinks first), so every attachment that does not already live in the
project's modeling workspace is copied, never linked, into its ``data/``
folder. ``materialize`` returns one line per input for the "Inputs:" list
appended to the agent's message.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

from . import cryosparc_jobs, files as file_roots
from .cryosparc_jobs import CryoSPARCError
from .state import store
from .store import NotFound

MAX_ATTACHMENTS = 24
CRYOSPARC_ROOT_PREFIX = "cryosparc:"


class AttachmentError(ValueError):
    pass


def materialize(project: dict[str, Any], attachments: list[dict[str, Any]]) -> list[str]:
    """Copy each attachment into the workspace and describe it for the agent."""

    if not attachments:
        return []
    if not isinstance(attachments, list):
        raise AttachmentError("Attachments must be a list.")
    if len(attachments) > MAX_ATTACHMENTS:
        raise AttachmentError(f"Attach at most {MAX_ATTACHMENTS} inputs to one message.")
    workspace = file_roots.modeling_workspace(project["slug"])
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace_relative = file_roots.modeling_workspace_relative(project["slug"])
    lines: list[str] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise AttachmentError("Each attachment must be an object.")
        kind = attachment.get("type")
        if kind == "cryosparc_map":
            line = _cryosparc_map(project, attachment, data_dir, workspace_relative)
        elif kind == "file":
            line = _project_file(project, attachment, workspace, data_dir, workspace_relative)
        else:
            raise AttachmentError(f"Unknown attachment type: {kind!r}.")
        if line not in lines:
            lines.append(line)
    return lines


# ----------------------------------------------------------- CryoSPARC maps
def _reconstruction_session(project: dict[str, Any], session_id: str, what: str) -> dict[str, Any]:
    try:
        session = store.get_session(session_id)
    except NotFound:
        raise AttachmentError(f"The reconstruction session behind {what} no longer exists.")
    if session["project_id"] != project["id"] or session["kind"] != "reconstruction":
        raise AttachmentError(f"{what} does not belong to a reconstruction session of this project.")
    return session


def _format_number(value: float, digits: int) -> str:
    return f"{value:.{digits}g}"


def _describe(project_uid: str, job: dict[str, Any], volume: dict[str, Any]) -> str:
    label = volume["label"]
    parts = [f"CryoSPARC {project_uid} {job['uid']}", job.get("type_title") or job.get("type") or "job", label[0].lower() + label[1:]]
    if volume.get("resolution_A"):
        resolution = f"{volume['resolution_A']:.2f} Å GSFSC"
        if volume.get("resolution_from"):
            resolution += f" from {volume['resolution_from']}"
        parts.append(resolution)
    if volume.get("b_factor") is not None:
        parts.append(f"B-factor {volume['b_factor']:.1f} Å²")
    if volume.get("psize_A"):
        parts.append(f"{_format_number(volume['psize_A'], 4)} Å/pixel")
    return ", ".join(parts)


def _cryosparc_map(project: dict[str, Any], attachment: dict[str, Any], data_dir: Path, workspace_relative: str) -> str:
    session_id = str(attachment.get("session_id") or "").strip()
    job_uid = str(attachment.get("job_uid") or "").strip()
    name = str(attachment.get("name") or "").strip()
    if not session_id or not job_uid or not name:
        raise AttachmentError("A CryoSPARC map needs session_id, job_uid, and name.")
    what = f"{job_uid} {name}"
    session = _reconstruction_session(project, session_id, what)
    try:
        source, volume, job, bound = cryosparc_jobs.volume_file(session, job_uid, name)
    except CryoSPARCError as exc:
        raise AttachmentError(f"Could not get {what}: {exc.message}")
    if volume.get("resolution_A") is None:
        # The job listing knows which refinement a sharpened or filtered map came from.
        described = cryosparc_jobs.describe_volume(session, job_uid, name)
        if described is not None:
            volume = {**volume, "resolution_A": described[0]["resolution_A"], "resolution_from": described[0]["resolution_from"]}
    target = data_dir / cryosparc_jobs.volume_filename(bound.project_uid, job_uid, volume["name"])
    _copy(source, target)
    return f"Density map: {workspace_relative}/data/{target.name} ({_describe(bound.project_uid, job, volume)})"


# ------------------------------------------------------------ project files
def _category_label(name: str) -> str:
    if name.lower().endswith(file_roots.SEQUENCE_EXTENSIONS):
        return "Sequence (FASTA)"
    return {"structure": "Model", "volume": "Density map"}.get(file_roots.file_category(name), "File")


def _cryosparc_root_file(project: dict[str, Any], root: str, path: str) -> tuple[Path, str]:
    """A file inside a session's CryoSPARC project folder, and the project uid."""

    session_id = root[len(CRYOSPARC_ROOT_PREFIX) :]
    session = _reconstruction_session(project, session_id, path)
    bound = cryosparc_jobs.binding(session)
    base = cryosparc_jobs.project_dir_for_session(session)
    if bound is None or base is None:
        raise AttachmentError("The CryoSPARC project folder of that session is not readable from this computer.")
    base = base.resolve()
    candidate = (base / path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise AttachmentError(f"{path} is outside the CryoSPARC project folder.")
    return candidate, bound.project_uid


def _project_file(project: dict[str, Any], attachment: dict[str, Any], workspace: Path, data_dir: Path, workspace_relative: str) -> str:
    root = str(attachment.get("root") or "").strip()
    path = str(attachment.get("path") or "").strip().lstrip("/")
    if not root or not path:
        raise AttachmentError("A file attachment needs root and path.")
    prefix = ""
    if root.startswith(CRYOSPARC_ROOT_PREFIX):
        source, project_uid = _cryosparc_root_file(project, root, path)
        prefix = f"{project_uid}_"
    else:
        try:
            source = file_roots.resolve(project["slug"], root, path)
        except ValueError as exc:
            raise AttachmentError(f"{path}: {exc}")
    if not source.is_file():
        raise AttachmentError(f"File not found: {path}")
    label = _category_label(source.name)
    if root == "modeling":
        # Already inside the agent's sandbox: refer to it where it is.
        relative = source.relative_to(workspace.resolve()).as_posix()
        return f"{label}: {workspace_relative}/{relative}"
    target = _target_for(data_dir, prefix + source.name, source)
    _copy(source, target)
    return f"{label}: {workspace_relative}/data/{target.name}"


# ------------------------------------------------------------------ copying
def _same_copy(source: Path, target: Path) -> bool:
    """True when ``target`` is a regular file copied from ``source`` (same size and time)."""

    if target.is_symlink() or not target.is_file():
        return False
    a, b = source.stat(), target.stat()
    return a.st_size == b.st_size and abs(a.st_mtime - b.st_mtime) < 1


def _target_for(data_dir: Path, name: str, source: Path) -> Path:
    """``data/<name>``, or ``data/<stem>-2<suffix>`` and so on if a different file has that name."""

    candidate = data_dir / name
    if not candidate.exists() or _same_copy(source, candidate) or candidate.resolve() == source.resolve():
        return candidate
    for index in range(2, 1000):
        candidate = data_dir / file_roots.numbered_name(name, index)
        if not candidate.exists() or _same_copy(source, candidate):
            return candidate
    raise AttachmentError(f"Too many files named {name} in the workspace data folder.")


def _copy(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target`` unless an identical copy is already there."""

    if source.resolve() == target.resolve() and not target.is_symlink():
        return
    if _same_copy(source, target):
        return
    partial = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        shutil.copy2(source, partial)
        os.replace(partial, target)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise AttachmentError(f"Could not copy {source.name} into the workspace: {exc.strerror or exc}")
