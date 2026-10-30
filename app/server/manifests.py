"""Build and validate harness manifests from web-form input.

Produces exactly the schema-v1 manifest that ``startup_cli.collect_session_manifest``
would have produced from the terminal questionnaire.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Any

from .config import RECONSTRUCTION_AGENT_ROOT

if str(RECONSTRUCTION_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(RECONSTRUCTION_AGENT_ROOT))

from llm_client import PROVIDERS, provider_for_model  # noqa: E402
from startup_cli import (  # noqa: E402
    DATASET_KINDS,
    EXISTING_PROJECT,
    MANIFEST_SCHEMA_VERSION,
    WORKSPACE_TITLE,
    continuation_manifest,
)

# CryoSPARC project UIDs: P followed by a number.
PROJECT_UID = re.compile(r"P\d+")


class ManifestError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {value}" for key, value in errors.items()))
        self.errors = errors


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _required_text(form: dict[str, Any], key: str, errors: dict[str, str], *, preserve: bool = False) -> str:
    value = _text(form.get(key))
    if not value.strip():
        errors[key] = "Required"
        return ""
    return value if preserve else value.strip()


def _float(
    form: dict[str, Any],
    key: str,
    errors: dict[str, str],
    *,
    minimum: float = 0.0,
    inclusive: bool = False,
    optional: bool = False,
) -> float | None:
    raw = form.get(key)
    if raw is None or _text(raw).strip() == "":
        if optional:
            return None
        errors[key] = "Required"
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        errors[key] = "Enter a number"
        return None
    if math.isnan(value) or math.isinf(value):
        errors[key] = "Enter a finite number"
        return None
    valid = value >= minimum if inclusive else value > minimum
    if not valid:
        relation = "at least" if inclusive else "greater than"
        errors[key] = f"Must be {relation} {minimum:g}"
        return None
    return value


def _positive_int(form: dict[str, Any], key: str, errors: dict[str, str]) -> int | None:
    raw = form.get(key)
    try:
        value = int(str(raw).strip(), 10)
    except (TypeError, ValueError):
        errors[key] = "Enter a positive integer"
        return None
    if value <= 0:
        errors[key] = "Enter a positive integer"
        return None
    return value


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).strip().lower() in {"1", "true", "yes", "on"}


def build_continuation_manifest(form: dict[str, Any]) -> dict[str, Any]:
    """The manifest of a session that continues an existing CryoSPARC project."""

    errors: dict[str, str] = {}
    project_uid = _text(form.get("existing_project_uid")).strip().upper()
    if not project_uid:
        errors["existing_project_uid"] = "Choose a CryoSPARC project"
    elif not PROJECT_UID.fullmatch(project_uid):
        errors["existing_project_uid"] = "Enter a project UID such as P12"
    project_title = _text(form.get("existing_project_title")).strip() or project_uid
    particle_diameter = _float(form, "particle_diameter_A", errors, optional=True)
    molecular_mass = _float(form, "molecular_mass_kDa", errors, optional=True)
    user_goal = _required_text(form, "user_goal", errors)
    if errors:
        raise ManifestError(errors)
    return continuation_manifest(
        project_uid=project_uid,
        project_title=project_title,
        user_goal=user_goal,
        sample_description=_text(form.get("sample_description")).strip(),
        particle_diameter_A=particle_diameter,
        molecular_mass_kDa=molecular_mass,
        expected_symmetry=_text(form.get("expected_symmetry")).strip() or "C1",
    )


def build_manifest(form: dict[str, Any]) -> dict[str, Any]:
    if _text(form.get("dataset_kind")).strip().lower() == EXISTING_PROJECT:
        return build_continuation_manifest(form)
    errors: dict[str, str] = {}

    project_title = _required_text(form, "project_title", errors)
    parent_directory = _required_text(form, "parent_directory", errors, preserve=True)
    dataset_kind = _text(form.get("dataset_kind")).strip().lower()
    if dataset_kind not in DATASET_KINDS:
        errors["dataset_kind"] = f"Choose one of {', '.join(DATASET_KINDS)}"

    particle_metadata_path = None
    if dataset_kind == "particles":
        particle_metadata_path = _required_text(form, "particle_metadata_path", errors, preserve=True)
    dataset_path = _required_text(form, "dataset_path", errors, preserve=True)

    pixel_size = _float(form, "pixel_size_A", errors)
    voltage = _float(form, "accelerating_voltage_kV", errors)
    spherical_aberration = _float(form, "spherical_aberration_mm", errors, minimum=0.0, inclusive=True)
    total_dose = _float(form, "total_dose_e_per_A2", errors)

    gain_reference_path = None
    defect_file_path = None
    eer_enabled = False
    eer_fractions = None
    eer_upsampling = None
    if dataset_kind == "movies":
        gain_reference_path = _text(form.get("gain_reference_path")) or None
        if gain_reference_path is not None and not gain_reference_path.strip():
            gain_reference_path = None
        defect_file_path = _text(form.get("defect_file_path")) or None
        if defect_file_path is not None and not defect_file_path.strip():
            defect_file_path = None
        eer_enabled = _bool(form.get("eer_enabled"))
        if eer_enabled:
            eer_fractions = _positive_int(form, "eer_fractions", errors)
            eer_upsampling = _positive_int(form, "eer_upsampling", errors)

    negative_stain = _bool(form.get("negative_stain"))
    phase_plate = _bool(form.get("phase_plate"))
    sample_description = _text(form.get("sample_description")).strip()
    particle_diameter = _float(form, "particle_diameter_A", errors)
    molecular_mass = _float(form, "molecular_mass_kDa", errors, optional=True)
    symmetry = _text(form.get("expected_symmetry")).strip() or "C1"
    user_goal = _required_text(form, "user_goal", errors)

    if errors:
        raise ManifestError(errors)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "session": {"id": None},
        "project": {
            "title": project_title,
            "parent_directory": parent_directory,
            "project_uid": None,
            "workspace_title": WORKSPACE_TITLE,
            "workspace_uid": None,
        },
        "dataset": {
            "kind": dataset_kind,
            "path": dataset_path,
            "particle_metadata_path": particle_metadata_path,
            "gain_reference_path": gain_reference_path,
            "defect_file_path": defect_file_path,
            "eer": {
                "enabled": eer_enabled,
                "fractions": eer_fractions,
                "upsampling": eer_upsampling,
            },
        },
        "acquisition": {
            "pixel_size_A": pixel_size,
            "accelerating_voltage_kV": voltage,
            "spherical_aberration_mm": spherical_aberration,
            "total_dose_e_per_A2": total_dose,
            "negative_stain": negative_stain,
            "phase_plate": phase_plate,
        },
        "sample": {
            "description": sample_description,
            "particle_diameter_A": particle_diameter,
            "molecular_mass_kDa": molecular_mass,
            "expected_symmetry": symmetry,
        },
        "user_goal": user_goal,
    }


def build_agent_config(form: dict[str, Any]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    model = _text(form.get("model")).strip()
    provider = _text(form.get("provider")).strip().lower() or provider_for_model(model)
    if provider not in PROVIDERS:
        errors["provider"] = "Choose openai, anthropic, or ollama"
    reasoning_effort = _text(form.get("reasoning_effort")).strip().lower() or None
    if reasoning_effort and reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        errors["reasoning_effort"] = "Unknown reasoning effort"
    config: dict[str, Any] = {"provider": provider}
    if model:
        config["model"] = model
    if reasoning_effort:
        config["reasoning_effort"] = reasoning_effort
    for key in ("rlm_max_depth", "rlm_max_calls", "rlm_max_model_turns"):
        raw = form.get(key)
        if raw is None or _text(raw).strip() == "":
            continue
        value = _positive_int({key: raw}, key, errors)
        if value is not None:
            config[key] = value
    if errors:
        raise ManifestError(errors)
    return config


def compose_modeling_request(form: dict[str, Any], workspace_relative: str) -> str:
    """Turn the modeling form into the agent's first natural-language request."""

    objective = _text(form.get("objective")).strip()
    lines = [objective] if objective else []
    inputs = []
    for key, label in (
        ("map_path", "Density map"),
        ("model_path", "Model"),
        ("protein_fasta_path", "Protein FASTA"),
        ("dna_fasta_path", "DNA FASTA"),
        ("rna_fasta_path", "RNA FASTA"),
    ):
        value = _text(form.get(key)).strip()
        if value:
            path = value if value.startswith(workspace_relative) else str(Path(workspace_relative) / value)
            inputs.append(f"- {label}: {path}")
    contour = _text(form.get("contour_level")).strip()
    if contour:
        inputs.append(f"- Suggested contour level: {contour} (confirm it with the visual contour inspection)")
    resolution = _text(form.get("resolution_A")).strip()
    if resolution:
        inputs.append(f"- Map resolution: {resolution} Å")
    if inputs:
        lines.append("")
        lines.append("Inputs:")
        lines.extend(inputs)
    return "\n".join(lines).strip()
