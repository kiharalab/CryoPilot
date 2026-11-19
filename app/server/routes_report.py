"""A session's run report and its transcript, to read in the app or download."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response, StreamingResponse

from . import report
from .state import require_session

router = APIRouter()


def _attachment(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"}


@router.get("/api/sessions/{session_id}/report")
def get_report(session_id: str) -> dict[str, Any]:
    return report.build_report(require_session(session_id))


@router.get("/api/sessions/{session_id}/report.md")
def download_report(session_id: str) -> Response:
    built = report.build_report(require_session(session_id))
    return Response(built["markdown"], media_type="text/markdown; charset=utf-8", headers=_attachment(built["filename"]))


@router.get("/api/sessions/{session_id}/transcript.md")
def download_transcript(session_id: str) -> StreamingResponse:
    session = require_session(session_id)
    return StreamingResponse(
        report.transcript_markdown(session),
        media_type="text/markdown; charset=utf-8",
        headers=_attachment(report.transcript_filename(session, "md")),
    )


@router.get("/api/sessions/{session_id}/transcript.jsonl")
def download_transcript_records(session_id: str) -> StreamingResponse:
    session = require_session(session_id)
    return StreamingResponse(
        report.transcript_jsonl(session),
        media_type="application/x-ndjson; charset=utf-8",
        headers=_attachment(report.transcript_filename(session, "jsonl")),
    )
