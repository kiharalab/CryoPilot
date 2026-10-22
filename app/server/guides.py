"""The reconstruction agent's processing guides, for the Settings page.

The harness picks its guide from ``CRYOEM_GUIDE`` when it is imported
(``harness.CRYOEM_GUIDES``), so the app selects a guide by setting that
variable in the driver's environment. The keys here match the harness keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import REPO_ROOT

GUIDE_FILES = {
    "original": "cryoem-data-processing.md",
    "v5": "cryoem-data-processing-v5.md",
}
DEFAULT_GUIDE = "original"
GUIDE_LABELS = {
    "original": "General guide",
    "v5": "CryoSPARC v5 guide",
}
GUIDE_DESCRIPTIONS = {
    "original": (
        "A target-agnostic framework for single-particle processing: start from the biology, "
        "then let diagnostics choose each processing branch."
    ),
    "v5": (
        "The same framework written for CryoSPARC v5.0, with v5 job and parameter names and "
        "starting values the agent checks against the installed job specifications."
    ),
}


def guide_path(guide_id: str) -> Path:
    if guide_id not in GUIDE_FILES:
        raise KeyError(guide_id)
    return REPO_ROOT / GUIDE_FILES[guide_id]


def normalize(guide_id: str | None) -> str:
    guide_id = str(guide_id or "").strip().lower()
    return guide_id if guide_id in GUIDE_FILES else DEFAULT_GUIDE


def _summary(text: str) -> dict[str, Any]:
    title = ""
    description = ""
    sections: list[str] = []
    paragraph: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            continue
        if stripped.startswith("## "):
            sections.append(stripped[3:].strip())
            continue
        if not description and title and not sections:
            if stripped:
                paragraph.append(stripped)
            elif paragraph:
                description = " ".join(paragraph)
    if not description and paragraph:
        description = " ".join(paragraph)
    return {"title": title, "description": description, "sections": sections}


def list_guides() -> list[dict[str, Any]]:
    guides = []
    for guide_id in GUIDE_FILES:
        path = guide_path(guide_id)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            guides.append({"id": guide_id, "label": GUIDE_LABELS[guide_id], "available": False, "path": str(path)})
            continue
        guides.append(
            {
                "id": guide_id,
                "label": GUIDE_LABELS[guide_id],
                "available": True,
                "path": str(path),
                "characters": len(text),
                **_summary(text),
                "description": GUIDE_DESCRIPTIONS[guide_id],
            }
        )
    return guides


def read_guide(guide_id: str) -> dict[str, Any]:
    path = guide_path(guide_id)
    text = path.read_text(encoding="utf-8")
    return {"id": guide_id, "label": GUIDE_LABELS[guide_id], "path": str(path), "markdown": text}
