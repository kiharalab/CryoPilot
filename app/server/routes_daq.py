"""DAQ results in a project's modeling workspace."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from . import daq
from . import files as file_roots
from .state import api_error, require_project

router = APIRouter()


def _workspace(project_id: str) -> Path:
    project = require_project(project_id)
    file_roots.ensure_project_dirs(project["slug"])
    return file_roots.modeling_workspace(project["slug"])


def _check_window(window: int) -> int:
    try:
        return daq.check_window(window)
    except daq.DaqError as exc:
        raise api_error(400, str(exc))


@router.get("/api/projects/{project_id}/daq")
def daq_results(project_id: str) -> dict[str, Any]:
    return {"results": daq.list_results(_workspace(project_id))}


@router.get("/api/projects/{project_id}/daq/scores")
def daq_scores(project_id: str, path: str, window: int = daq.DEFAULT_WINDOW) -> dict[str, Any]:
    workspace = _workspace(project_id)
    window = _check_window(window)
    try:
        return daq.scores(workspace, path, window)
    except FileNotFoundError as exc:
        raise api_error(404, str(exc))
    except (ValueError, csv.Error) as exc:  # DaqError, a path outside the workspace, or a malformed table
        raise api_error(400, str(exc))


@router.get("/api/projects/{project_id}/daq/structure")
def daq_structure(project_id: str, path: str, metric: str = "aa_mean", window: int = daq.DEFAULT_WINDOW) -> PlainTextResponse:
    workspace = _workspace(project_id)
    window = _check_window(window)
    try:
        text, filename = daq.scored_structure(workspace, path, metric, window)
    except FileNotFoundError as exc:
        raise api_error(404, str(exc))
    except (ValueError, csv.Error) as exc:
        raise api_error(400, str(exc))
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-cache"},
    )
