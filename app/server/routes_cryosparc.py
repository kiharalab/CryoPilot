"""CryoSPARC jobs, maps, and image assets for reconstruction sessions.

These are plain ``def`` endpoints, so FastAPI runs them in its thread pool:
CryoSPARC API calls, map downloads, and preview binning never block the
event loop.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from . import cryosparc_jobs, volumes
from .cryosparc_jobs import CryoSPARCError
from .state import api_error, require_project, require_session

router = APIRouter()

# CryoSPARC writes these images itself, but an SVG served from this origin
# could still carry script, so assets get a policy that allows none.
ASSET_HEADERS = {
    "Cache-Control": "private, max-age=86400",
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


def _reconstruction_session(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    if session["kind"] != "reconstruction":
        raise api_error(400, "Only reconstruction sessions have CryoSPARC jobs.")
    return session


def _error(exc: CryoSPARCError):
    return api_error(exc.status, exc.message, **exc.extra)


@router.get("/api/sessions/{session_id}/cryosparc/jobs")
def list_cryosparc_jobs(session_id: str) -> dict[str, Any]:
    session = _reconstruction_session(session_id)
    try:
        return cryosparc_jobs.list_jobs(session)
    except CryoSPARCError as exc:
        raise _error(exc)


@router.get("/api/sessions/{session_id}/cryosparc/volume")
def get_cryosparc_volume(session_id: str, job: str, name: str, preview: int = 1) -> FileResponse:
    session = _reconstruction_session(session_id)
    try:
        path, volume, _job, bound = cryosparc_jobs.volume_file(session, job, name)
    except CryoSPARCError as exc:
        raise _error(exc)
    filename = cryosparc_jobs.volume_filename(bound.project_uid, job, volume["name"])
    headers = {"Cache-Control": "private, max-age=300"}
    if preview:
        try:
            served, stats = volumes.preview(path)
        except (volumes.VolumeError, OSError, ValueError) as exc:
            raise api_error(422, f"Could not read {filename}: {exc}")
        headers["X-Map-Stats"] = volumes.stats_header(stats)
        if served != path:
            filename = cryosparc_jobs.volume_filename(bound.project_uid, job, volume["name"], "_preview")
        path = served
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers=headers,
        content_disposition_type="inline",
    )


@router.get("/api/sessions/{session_id}/cryosparc/jobs/{job_uid}/assets")
def list_cryosparc_assets(session_id: str, job_uid: str) -> dict[str, Any]:
    session = _reconstruction_session(session_id)
    try:
        return cryosparc_jobs.list_assets(session, job_uid)
    except CryoSPARCError as exc:
        raise _error(exc)


@router.get("/api/sessions/{session_id}/cryosparc/jobs/{job_uid}/assets/{asset_id}")
def get_cryosparc_asset(session_id: str, job_uid: str, asset_id: str) -> FileResponse:
    session = _reconstruction_session(session_id)
    try:
        path, media_type, filename = cryosparc_jobs.asset_file(session, job_uid, asset_id)
    except CryoSPARCError as exc:
        raise _error(exc)
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers=ASSET_HEADERS,
        content_disposition_type="inline",
    )


@router.get("/api/projects/{project_id}/maps")
def list_project_maps(project_id: str, include_all: int = Query(0, alias="all")) -> dict[str, Any]:
    project = require_project(project_id)
    return cryosparc_jobs.project_maps(project, include_all=bool(include_all))
