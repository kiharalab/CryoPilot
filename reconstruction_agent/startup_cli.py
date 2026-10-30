"""Interactive startup helpers for a single-project cryo-EM agent session.

The functions in this module deliberately do not connect to CryoSPARC or mutate
remote state.  They collect and validate startup input, construct the persisted
session manifest, and produce deterministic prompts once a project/workspace
scope has been bound by the launcher.
"""

from __future__ import annotations

import copy
import getpass
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit


InputFunction = Callable[[str], str]
SecretInputFunction = Callable[[str], str]

MANIFEST_SCHEMA_VERSION = 1
WORKSPACE_TITLE = "Automated Reconstruction"
DATASET_KINDS = ("movies", "micrographs", "particles")
# A session that continues a CryoSPARC project someone has already started:
# people or earlier agent sessions. It imports nothing and works in a new
# workspace of its own.
EXISTING_PROJECT = "existing_project"
# Notes from each earlier session that the first prompt quotes, at most.
EARLIER_SESSION_NOTE_CHARS = 3000

_IMPORT_JOBS = {
    "movies": ("Import Movies", "csw.import_movies"),
    "micrographs": ("Import Micrographs", "csw.import_micrographs"),
    "particles": ("Import Particle Stack", "csw.import_particle_stack"),
}


def _default_input(prompt: str) -> str:
    return input(prompt)


def _default_getpass(prompt: str) -> str:
    return getpass.getpass(prompt)


def prompt_required(
    prompt: str,
    *,
    input_fn: InputFunction = _default_input,
    preserve: bool = False,
) -> str:
    """Prompt until non-whitespace text is entered.

    ``preserve=True`` is intended for server-visible paths and globs.  It checks
    emptiness using ``strip`` but returns the original string byte-for-byte.
    """

    while True:
        value = input_fn(prompt)
        if value.strip():
            return value if preserve else value.strip()
        print("A value is required.")


def prompt_optional(
    prompt: str,
    *,
    input_fn: InputFunction = _default_input,
    preserve: bool = False,
) -> str | None:
    """Return optional input, using ``None`` for an empty/whitespace response."""

    value = input_fn(prompt)
    if not value.strip():
        return None
    return value if preserve else value.strip()


def prompt_choice(
    prompt: str,
    choices: tuple[str, ...] | list[str],
    *,
    input_fn: InputFunction = _default_input,
) -> str:
    """Prompt until one of the case-insensitive choices is entered."""

    normalized = {str(choice).lower(): str(choice) for choice in choices}
    if not normalized:
        raise ValueError("choices must not be empty")
    while True:
        value = input_fn(prompt).strip().lower()
        if value in normalized:
            return normalized[value]
        print(f"Choose one of: {', '.join(normalized.values())}.")


def prompt_yes_no(
    prompt: str,
    *,
    input_fn: InputFunction = _default_input,
    default: bool | None = None,
) -> bool:
    """Prompt for yes/no, accepting y/yes/n/no and an optional default."""

    while True:
        value = input_fn(prompt).strip().lower()
        if not value and default is not None:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter yes or no.")


def prompt_float(
    prompt: str,
    *,
    input_fn: InputFunction = _default_input,
    minimum: float = 0.0,
    inclusive: bool = False,
) -> float:
    """Prompt for a finite float above (or at) ``minimum``."""

    while True:
        raw = input_fn(prompt).strip()
        try:
            value = float(raw)
        except ValueError:
            print("Enter a number.")
            continue

        valid = value >= minimum if inclusive else value > minimum
        # NaN fails both comparisons; reject infinities explicitly.
        if valid and value not in {float("inf"), float("-inf")}:
            return value
        relation = "at least" if inclusive else "greater than"
        print(f"Enter a finite number {relation} {minimum:g}.")


def prompt_optional_positive_float(
    prompt: str,
    *,
    input_fn: InputFunction = _default_input,
) -> float | None:
    """Prompt for an optional finite positive float."""

    while True:
        raw = input_fn(prompt).strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            print("Enter a positive number or leave blank.")
            continue
        if value > 0 and value != float("inf"):
            return value
        print("Enter a finite positive number or leave blank.")


def prompt_positive_int(
    prompt: str,
    *,
    input_fn: InputFunction = _default_input,
) -> int:
    """Prompt for a positive base-10 integer."""

    while True:
        raw = input_fn(prompt).strip()
        try:
            value = int(raw, 10)
        except ValueError:
            print("Enter a positive integer.")
            continue
        if value > 0:
            return value
        print("Enter a positive integer.")


def collect_cryosparc_credentials(
    *,
    url: str | None = None,
    email: str | None = None,
    password: str | None = None,
    input_fn: InputFunction = _default_input,
    getpass_fn: SecretInputFunction = _default_getpass,
) -> dict[str, str]:
    """Collect missing CryoSPARC connection values without persisting them.

    Flag/environment values can be passed by the launcher.  Only missing values
    are prompted for, and the password prompt is injectable for tests.
    """

    resolved_url = url if url is not None and url.strip() else None
    resolved_email = email if email is not None and email.strip() else None
    resolved_password = password if password is not None and password.strip() else None

    while True:
        if resolved_url is None:
            resolved_url = prompt_required("CryoSPARC URL: ", input_fn=input_fn)
        else:
            resolved_url = resolved_url.strip()
        parsed_url = urlsplit(
            resolved_url if "://" in resolved_url else f"//{resolved_url}"
        )
        if parsed_url.username is None and parsed_url.password is None:
            break
        if url is not None and url.strip():
            raise ValueError(
                "CryoSPARC URL must not contain embedded user-info credentials."
            )
        print("Enter a CryoSPARC URL without embedded user-info credentials.")
        resolved_url = None
    if resolved_email is None:
        resolved_email = prompt_required("CryoSPARC email: ", input_fn=input_fn)
    else:
        resolved_email = resolved_email.strip()
    while resolved_password is None:
        candidate = getpass_fn("CryoSPARC password: ")
        if candidate.strip():
            resolved_password = candidate
        else:
            print("A password is required.")

    return {
        "url": resolved_url,
        "email": resolved_email,
        "password": resolved_password,
    }


def collect_session_manifest(
    *,
    input_fn: InputFunction = _default_input,
) -> dict[str, Any]:
    """Run the startup questionnaire and return an unbound schema-v1 manifest."""

    project_title = prompt_required("Project title: ", input_fn=input_fn)
    parent_directory = prompt_required(
        "Server-visible CryoSPARC project parent directory: ",
        input_fn=input_fn,
        preserve=True,
    )
    dataset_kind = prompt_choice(
        "Dataset kind [movies/micrographs/particles]: ",
        DATASET_KINDS,
        input_fn=input_fn,
    )
    particle_metadata_path = None
    if dataset_kind == "particles":
        particle_metadata_path = prompt_required(
            "Server-visible particle metadata path (.star): ",
            input_fn=input_fn,
            preserve=True,
        )
        dataset_path_prompt = "Server-visible particle data path or glob: "
    else:
        dataset_path_prompt = "Server-visible dataset path or glob: "
    dataset_path = prompt_required(
        dataset_path_prompt,
        input_fn=input_fn,
        preserve=True,
    )

    pixel_size = prompt_float("Pixel size (A/pixel): ", input_fn=input_fn)
    voltage = prompt_float("Accelerating voltage (kV): ", input_fn=input_fn)
    spherical_aberration = prompt_float(
        "Spherical aberration (mm): ",
        input_fn=input_fn,
        minimum=0.0,
        inclusive=True,
    )
    total_dose = prompt_float("Total exposure dose (e-/A^2): ", input_fn=input_fn)

    gain_reference_path = None
    defect_file_path = None
    eer_enabled = False
    eer_fractions = None
    eer_upsampling = None
    if dataset_kind == "movies":
        gain_reference_path = prompt_optional(
            "Gain reference path (optional): ",
            input_fn=input_fn,
            preserve=True,
        )
        defect_file_path = prompt_optional(
            "Defect file path (optional): ",
            input_fn=input_fn,
            preserve=True,
        )
        eer_enabled = prompt_yes_no(
            "Is this an EER dataset? [y/N]: ",
            input_fn=input_fn,
            default=False,
        )
        if eer_enabled:
            eer_fractions = prompt_positive_int(
                "EER fractions: ",
                input_fn=input_fn,
            )
            eer_upsampling = prompt_positive_int(
                "EER upsampling factor: ",
                input_fn=input_fn,
            )

    negative_stain = prompt_yes_no(
        "Negative-stain dataset? [y/N]: ",
        input_fn=input_fn,
        default=False,
    )
    phase_plate = prompt_yes_no(
        "Phase plate used? [y/N]: ",
        input_fn=input_fn,
        default=False,
    )
    sample_description = input_fn("Sample or target description (optional): ").strip()
    particle_diameter = prompt_float(
        "Expected particle diameter (A): ",
        input_fn=input_fn,
    )
    molecular_mass = prompt_optional_positive_float(
        "Molecular mass (kDa, optional): ",
        input_fn=input_fn,
    )
    symmetry = input_fn("Expected symmetry [C1]: ").strip() or "C1"
    user_goal = prompt_required(
        "Scientific objective (for example, heterogeneity or rare particles): ",
        input_fn=input_fn,
    )

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


def continuation_manifest(
    *,
    project_uid: str,
    project_title: str,
    user_goal: str,
    sample_description: str = "",
    particle_diameter_A: float | None = None,
    molecular_mass_kDa: float | None = None,
    expected_symmetry: str = "C1",
) -> dict[str, Any]:
    """An unbound manifest for a session that continues an existing project.

    The dataset and acquisition values stay empty: the agent reads them from
    the project's own import and CTF jobs.
    """

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "session": {"id": None},
        "project": {
            "title": project_title,
            "parent_directory": None,
            "project_uid": project_uid,
            "workspace_title": WORKSPACE_TITLE,
            "workspace_uid": None,
        },
        "dataset": {
            "kind": EXISTING_PROJECT,
            "path": None,
            "particle_metadata_path": None,
            "gain_reference_path": None,
            "defect_file_path": None,
            "eer": {"enabled": False, "fractions": None, "upsampling": None},
        },
        "acquisition": {
            "pixel_size_A": None,
            "accelerating_voltage_kV": None,
            "spherical_aberration_mm": None,
            "total_dose_e_per_A2": None,
            "negative_stain": False,
            "phase_plate": False,
        },
        "sample": {
            "description": sample_description,
            "particle_diameter_A": particle_diameter_A,
            "molecular_mass_kDa": molecular_mass_kDa,
            "expected_symmetry": expected_symmetry or "C1",
        },
        "user_goal": user_goal,
    }


def collect_continuation_manifest(
    project_uid: str,
    project_title: str,
    *,
    input_fn: InputFunction = _default_input,
) -> dict[str, Any]:
    """Ask for the sample and the goal of a session that continues ``project_uid``."""

    sample_description = input_fn("Sample or target description (optional): ").strip()
    particle_diameter = prompt_optional_positive_float(
        "Expected particle diameter (A, optional): ",
        input_fn=input_fn,
    )
    molecular_mass = prompt_optional_positive_float(
        "Molecular mass (kDa, optional): ",
        input_fn=input_fn,
    )
    symmetry = input_fn("Expected symmetry [C1]: ").strip() or "C1"
    user_goal = prompt_required(
        "Scientific objective for this session: ",
        input_fn=input_fn,
    )
    return continuation_manifest(
        project_uid=project_uid,
        project_title=project_title,
        user_goal=user_goal,
        sample_description=sample_description,
        particle_diameter_A=particle_diameter,
        molecular_mass_kDa=molecular_mass,
        expected_symmetry=symmetry,
    )


def bind_manifest_scope(
    manifest: Mapping[str, Any],
    *,
    session_id: str,
    project_uid: str,
    workspace_uid: str,
) -> dict[str, Any]:
    """Return a deep-copied manifest bound to one persisted CryoSPARC scope."""

    bound = copy.deepcopy(dict(manifest))
    if bound.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"Unsupported manifest schema: {bound.get('schema_version')!r}")
    if bound.get("project", {}).get("workspace_title") != WORKSPACE_TITLE:
        raise ValueError(f"Workspace title must be {WORKSPACE_TITLE!r}")

    values = {
        "session_id": session_id,
        "project_uid": project_uid,
        "workspace_uid": workspace_uid,
    }
    normalized = {}
    for name, value in values.items():
        value = str(value).strip()
        if not value:
            raise ValueError(f"{name} must not be empty")
        normalized[name] = value

    bound["session"]["id"] = normalized["session_id"]
    bound["project"]["project_uid"] = normalized["project_uid"]
    bound["project"]["workspace_uid"] = normalized["workspace_uid"]
    return bound


def canonical_manifest_json(manifest: Mapping[str, Any]) -> str:
    """Serialize a manifest deterministically for persistence and prompting."""

    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)


def _bound_scope(manifest: Mapping[str, Any]) -> tuple[str, str, str]:
    try:
        session_id = str(manifest["session"]["id"]).strip()
        project_uid = str(manifest["project"]["project_uid"]).strip()
        workspace_uid = str(manifest["project"]["workspace_uid"]).strip()
    except (KeyError, TypeError) as exc:
        raise ValueError("Manifest is missing its bound session/project/workspace scope") from exc
    if not session_id or session_id == "None":
        raise ValueError("Manifest session ID is not bound")
    if not project_uid or project_uid == "None":
        raise ValueError("Manifest project UID is not bound")
    if not workspace_uid or workspace_uid == "None":
        raise ValueError("Manifest workspace UID is not bound")
    return session_id, project_uid, workspace_uid


def _continues_project(manifest: Mapping[str, Any]) -> bool:
    dataset = manifest.get("dataset")
    return isinstance(dataset, Mapping) and dataset.get("kind") == EXISTING_PROJECT


def _clip(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _earlier_session_notes(sessions: Sequence[Mapping[str, Any]]) -> str:
    if not sessions:
        return ""
    lines = [
        "Earlier CryoPilot sessions worked in this project. Their notes follow, newest "
        "first. They describe intent; check every claim against the jobs themselves."
    ]
    for session in sessions:
        state = ", ".join(str(value) for value in (session.get("model"), session.get("status")) if value)
        lines.append(f"\n- Session {session.get('id')} ({state}; last active {session.get('updated_at')})")
        if session.get("last_error"):
            lines.append(f"  It stopped with this error: {_clip(session['last_error'], 500)}")
        if session.get("summary"):
            lines.append(f"  Its last context summary:\n{_clip(session['summary'], EARLIER_SESSION_NOTE_CHARS)}")
        if session.get("last_message"):
            lines.append(f"  Its last message:\n{_clip(session['last_message'], EARLIER_SESSION_NOTE_CHARS)}")
    return "\n".join(lines) + "\n\n"


def _import_job_for_dataset(manifest: Mapping[str, Any]) -> tuple[str, str]:
    dataset = manifest.get("dataset")
    dataset_kind = dataset.get("kind") if isinstance(dataset, Mapping) else None
    try:
        return _IMPORT_JOBS[str(dataset_kind)]
    except KeyError as exc:
        raise ValueError(f"Unsupported dataset kind: {dataset_kind!r}") from exc


def build_developer_instruction(manifest: Mapping[str, Any]) -> str:
    """Build deterministic runtime instructions pinned to the bound scope."""

    session_id, project_uid, workspace_uid = _bound_scope(manifest)
    if _continues_project(manifest):
        return (
            "You are an interactive scientific agent performing single-particle cryo-EM "
            "reconstruction in the CryoSPARC scope below.\n"
            f"Session ID: {session_id}\n"
            f"CryoSPARC project UID: {project_uid}\n"
            f"CryoSPARC workspace UID: {workspace_uid}\n"
            f"Workspace title: {WORKSPACE_TITLE}\n\n"
            f"This session continues project {project_uid}, which already holds work "
            "done by people or by earlier agent sessions. Workspace "
            f"{workspace_uid} is new and belongs to this session.\n"
            f"You may read every job in project {project_uid}, in any workspace: find "
            f'one with `csw.find_job(uid)`, list them with `csw.list_jobs(project_uid="{project_uid}", '
            f'limit=200)` or `csw.audit_jobs(project_uid="{project_uid}")`, and use the '
            "outputs of any of them as inputs with `csw.out(uid, output)`.\n"
            f"Create, queue, connect, clear, kill, or delete jobs only in workspace "
            f"{workspace_uid}. `csw` refuses to change a job in another workspace; never "
            "change or delete one by other means either. Do not create, select, or "
            "switch to another project or workspace. The connected `csw` object is "
            "already bound to this exact scope.\n"
            "Do not create an import job: the data is already in the project. Before "
            "you create any job, survey the project. List every job with its type, "
            "title, status, and workspace; inspect the key outputs (exposure and "
            "particle counts, class averages, maps, resolutions, and masks); take the "
            "acquisition values from the existing import and CTF jobs; and report what "
            "exists and what you plan to do next. A job's outputs are in `job.outputs`: "
            "for example, a refinement's FSC curves are in "
            "`job.outputs['volume'].latest_summary_stats['fsc_info_best']`, and "
            "`job.load_output(name)` loads an output's dataset. Then continue from the most advanced "
            "valid results instead of repeating finished steps. Repeat a step only when "
            "the evidence shows its result is wrong or unsuitable for the goal. Treat "
            "`user_goal` as the scientific objective. Pursue it conservatively, creating "
            "or processing only one or a few jobs at a time and inspecting their results "
            "before continuing."
        )
    import_job, import_wrapper = _import_job_for_dataset(manifest)
    particle_instruction = ""
    if manifest["dataset"]["kind"] == "particles":
        particle_instruction = (
            " This is an already-extracted particle stack, not a movie or micrograph "
            "dataset. Pass `dataset.particle_metadata_path` as `particle_meta_path` "
            "and `dataset.path` as `particle_data_path`. Preserve imported CTF, pose, "
            "and gold-standard subset metadata unless the scientific evidence requires "
            "otherwise."
        )
    return (
        "You are an interactive scientific agent performing single-particle cryo-EM "
        "reconstruction in the already-created CryoSPARC scope below.\n"
        f"Session ID: {session_id}\n"
        f"CryoSPARC project UID: {project_uid}\n"
        f"CryoSPARC workspace UID: {workspace_uid}\n"
        f"Workspace title: {WORKSPACE_TITLE}\n\n"
        "Operate only in that project and workspace. Do not create, select, or switch "
        "to another project or workspace. The connected `csw` object is already bound "
        "to this exact scope.\n"
        f"Create the appropriate {import_job} job with `{import_wrapper}(...)` from "
        "the session manifest; startup "
        "has not created or queued an import job. Inspect the installed live job spec "
        "and map every applicable acquisition, EER, gain-reference, and defect-file "
        "field without guessing version-specific parameter names."
        f"{particle_instruction} Treat `user_goal` "
        "as an additional scientific objective. Pursue it conservatively, creating or "
        "processing only one or a few jobs at a time and inspecting their results "
        "before continuing."
    )


def build_user_prompt(manifest: Mapping[str, Any], earlier_sessions: Sequence[Mapping[str, Any]] = ()) -> str:
    """Build the deterministic initial user prompt with canonical manifest JSON.

    ``earlier_sessions`` (for a session that continues a project) are notes
    from the project's earlier sessions, newest first: dicts with ``id``,
    ``model``, ``status``, ``updated_at``, ``last_error``, ``summary``, and
    ``last_message``.
    """

    session_id, project_uid, workspace_uid = _bound_scope(manifest)
    if _continues_project(manifest):
        return (
            f"Continue the cryo-EM reconstruction in CryoSPARC project {project_uid}. "
            "The launcher has already connected this session to the project and created "
            f"its own workspace {workspace_uid}; the data is already imported.\n\n"
            f"First survey the existing jobs in every workspace of project {project_uid} "
            "and report what they hold. Then work toward `user_goal`, creating new jobs "
            f"only in workspace {workspace_uid}, session {session_id}.\n\n"
            f"{_earlier_session_notes(earlier_sessions)}"
            "Session manifest (canonical JSON):\n"
            "```json\n"
            f"{canonical_manifest_json(manifest)}\n"
            "```"
        )
    import_job, import_wrapper = _import_job_for_dataset(manifest)
    return (
        "Begin the requested cryo-EM reconstruction session. The launcher has already "
        "created and connected the single allowed CryoSPARC project/workspace.\n\n"
        f"You must create the correct {import_job} job with `{import_wrapper}(...)` "
        "using the manifest below, and "
        "you must operate only in "
        f"project {project_uid}, workspace {workspace_uid}, session {session_id}. "
        "Treat `user_goal` as an additional scientific objective that guides the "
        "processing strategy.\n\n"
        "Session manifest (canonical JSON):\n"
        "```json\n"
        f"{canonical_manifest_json(manifest)}\n"
        "```"
    )


__all__ = [
    "DATASET_KINDS",
    "EARLIER_SESSION_NOTE_CHARS",
    "EXISTING_PROJECT",
    "MANIFEST_SCHEMA_VERSION",
    "WORKSPACE_TITLE",
    "bind_manifest_scope",
    "build_developer_instruction",
    "build_user_prompt",
    "canonical_manifest_json",
    "collect_continuation_manifest",
    "collect_cryosparc_credentials",
    "collect_session_manifest",
    "continuation_manifest",
    "prompt_choice",
    "prompt_float",
    "prompt_optional",
    "prompt_optional_positive_float",
    "prompt_positive_int",
    "prompt_required",
    "prompt_yes_no",
]
