"""CryoSPARC jobs, maps, and image assets of reconstruction sessions.

Two sources, one parser. When CryoSPARC credentials are set, job documents
come from the CryoSPARC API, which is live and authoritative. Otherwise they
come from each job's ``job.json`` in the project directory, which the server
can usually read because it runs on the CryoSPARC machine. ``parse_job``
reads both, in the CryoSPARC v5 layout (``spec.type``, ``spec.outputs``) and
the v4 layout (``job_type``, ``output_result_groups``, ``output_results``).

A map file's authoritative location is the ``<result>/path`` field of the
``.cs`` metafile CryoSPARC records for that output (the last metafile is the
latest version). Files are served from the project directory when it is
readable and downloaded through cryosparc-tools into ``APP_HOME/cache``
otherwise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from . import cryosparc_bridge, volumes
from .config import APP_HOME
from .state import cryosparc_credentials, store, trace_reader
from .trace import TraceError

log = logging.getLogger(__name__)

CACHE_DIR = APP_HOME / "cache" / "cryosparc"
MAP_CACHE_BYTES = 16 * 1024**3
ASSET_CACHE_BYTES = 512 * 1024**2
MAX_ASSETS = 200
ASSET_INDEX_SECONDS = 300
MISSING_PROJECT_DIR_SECONDS = 30

JOB_UID = re.compile(r"^J\d+$")
PROJECT_UID = re.compile(r"^P\d+$")
ASSET_ID = re.compile(r"^[0-9a-f]{24}$")
IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/svg+xml": "svg"}
IMAGE_EXTENSIONS = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".gif": "gif", ".svg": "svg"}
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "svg": "image/svg+xml"}

# Names CryoSPARC shows for job types; anything else is spelled out from its key.
JOB_TYPE_TITLES = {
    "import_movies": "Import Movies",
    "import_micrographs": "Import Micrographs",
    "import_particles": "Import Particle Stack",
    "import_volumes": "Import 3D Volumes",
    "patch_motion_correction_multi": "Patch Motion Correction",
    "patch_ctf_estimation_multi": "Patch CTF Estimation",
    "curate_exposures_v2": "Manually Curate Exposures",
    "blob_picker_gpu": "Blob Picker",
    "template_picker_gpu": "Template Picker",
    "filament_tracer_gpu": "Filament Tracer",
    "manual_picker": "Manual Picker",
    "topaz_train": "Topaz Train",
    "topaz_extract": "Topaz Extract",
    "inspect_picks_v2": "Inspect Particle Picks",
    "extract_micrographs_multi": "Extract From Micrographs",
    "extract_micrographs_cpu_parallel": "Extract From Micrographs (CPU)",
    "downsample_particles": "Downsample Particles",
    "restack_particles": "Restack Particles",
    "remove_duplicate_particles": "Remove Duplicate Particles",
    "class_2D_new": "2D Classification",
    "class_2D": "2D Classification (legacy)",
    "select_2D": "Select 2D Classes",
    "create_templates": "Create Templates",
    "homo_abinit": "Ab-Initio Reconstruction",
    "hetero_refine": "Heterogeneous Refinement",
    "homo_refine_new": "Homogeneous Refinement",
    "homo_refine": "Homogeneous Refinement (legacy)",
    "nonuniform_refine_new": "Non-uniform Refinement",
    "nonuniform_refine": "Non-uniform Refinement (legacy)",
    "new_local_refine": "Local Refinement",
    "local_refine": "Local Refinement (legacy)",
    "homo_reconstruct": "Homogeneous Reconstruction Only",
    "class_3D": "3D Classification",
    "var_3D": "3D Variability",
    "var_3D_disp": "3D Variability Display",
    "ctf_refine_global": "Global CTF Refinement",
    "ctf_refine_local": "Local CTF Refinement",
    "reference_motion_correction": "Reference Based Motion Correction",
    "symmetry_expansion": "Symmetry Expansion",
    "rebalance_orientations": "Rebalance Orientations",
    "orientation_diagnostics": "Orientation Diagnostics",
    "particle_sets": "Particle Sets Tool",
    "exposure_groups": "Exposure Group Utilities",
    "sharpen": "Sharpening Tools",
    "validation": "Validation (FSC)",
    "local_resolution": "Local Resolution Estimation",
    "local_filter": "Local Filtering",
    "volume_tools": "Volume Tools",
    "volume_alignment_tools": "Volume Alignment Tools",
    "align_3D_new": "Align 3D Maps",
    "flex_prep": "3D Flex Data Prep",
    "flex_meshprep": "3D Flex Mesh Prep",
    "flex_train": "3D Flex Training",
    "flex_highres": "3D Flex Reconstruction",
    "flex_generate": "3D Flex Generator",
}

# Post-processing jobs report no resolution of their own; they show the
# resolution of the refinement they were run on.
INHERITS_RESOLUTION = {"sharpen", "local_filter", "local_resolution", "volume_tools", "volume_alignment_tools"}

# result name -> (kind, label)
RESULT_KINDS = {
    "map": ("map", "Map"),
    "map_sharp": ("map_sharp", "Sharpened map"),
    "map_filtered": ("map_sharp", "Locally filtered map"),
    "map_half_A": ("half_map", "Half map A"),
    "map_half_B": ("half_map", "Half map B"),
    "mask": ("mask", "Mask"),
    "mask_refine": ("mask", "Mask (refinement)"),
    "mask_fsc": ("mask", "Mask (FSC)"),
    "mask_fsc_auto": ("mask", "Mask (FSC, auto-tightened)"),
    "precision": ("other", "Precision map"),
    "map_locres": ("other", "Local resolution map"),
    "map_3DFSC": ("other", "3D FSC map"),
}
KIND_ORDER = {"map_sharp": 0, "map": 1, "half_map": 2, "mask": 3, "other": 4}
MODELING_KINDS = ("map", "map_sharp")
CLASS_GROUP = re.compile(r"^volume_class_(\d+)$")


class CryoSPARCError(Exception):
    """A request about CryoSPARC data that cannot be answered; ``status`` is the HTTP status."""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class Binding:
    """The CryoSPARC project and workspace a reconstruction session works in."""

    session_id: str
    project_uid: str
    workspace_uid: str | None
    cryosparc_url: str | None
    parent_directory: str | None
    project_title: str | None
    harness_session_id: str | None


# ------------------------------------------------------------------ parsing
def _iso(value: Any) -> str | None:
    """A CryoSPARC timestamp (datetime, ISO text, or Mongo ``{"$date": ...}``) as UTC ISO 8601."""

    if isinstance(value, dict):
        value = value.get("$date")
        if isinstance(value, dict):
            value = value.get("$numberLong")
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.lstrip("-").isdigit()):
        moment = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _resolution(stats: dict[str, Any]) -> float | None:
    """GSFSC resolution (Å) from an output group's latest summary stats."""

    candidates = (
        (stats.get("fsc_info_best") or {}).get("radwn_final_A") if isinstance(stats.get("fsc_info_best"), dict) else None,
        (stats.get("fsc_info") or {}).get("radwn_final_A") if isinstance(stats.get("fsc_info"), dict) else None,
        stats.get("radwn_final_A"),
        stats.get("fsc_resolution"),
    )
    for candidate in candidates:
        number = _number(candidate)
        if number is not None and number > 0:
            return number
    return None


def job_type_title(job_type: str | None) -> str:
    if not job_type:
        return "Job"
    return JOB_TYPE_TITLES.get(job_type) or job_type.replace("_", " ").strip().capitalize()


def _volume_groups(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Volume output groups in one shape for both document layouts."""

    groups: list[dict[str, Any]] = []
    spec = doc.get("spec")
    if isinstance(spec, dict) and isinstance(spec.get("outputs"), dict):
        for name, output in spec["outputs"].items():
            if not isinstance(output, dict) or output.get("type") != "volume":
                continue
            groups.append(
                {
                    "name": name,
                    "title": output.get("title") or "",
                    "summary": output.get("summary") or {},
                    "stats": output.get("latest_summary_stats") or {},
                    "results": [
                        {
                            "name": result.get("name"),
                            "dtype": result.get("dtype"),
                            "metafiles": result.get("metafiles") or [],
                            "versions": result.get("versions") or [],
                            "passthrough": bool(result.get("passthrough")),
                        }
                        for result in output.get("results") or []
                        if isinstance(result, dict)
                    ],
                }
            )
        return groups
    results = [result for result in doc.get("output_results") or [] if isinstance(result, dict)]
    for group in doc.get("output_result_groups") or []:
        if not isinstance(group, dict) or group.get("type") != "volume":
            continue
        groups.append(
            {
                "name": group.get("name"),
                "title": group.get("title") or "",
                "summary": group.get("summary") or {},
                "stats": group.get("latest_summary_stats") or {},
                "results": [
                    {
                        "name": result.get("name"),
                        "dtype": str(result.get("type") or "").rsplit(".", 1)[-1],
                        "metafiles": result.get("metafiles") or [],
                        "versions": result.get("versions") or [],
                        "passthrough": bool(result.get("passthrough")),
                    }
                    for result in results
                    if result.get("group_name") == group.get("name")
                ],
            }
        )
    return groups


def _label(result: str, group: str, group_has_sharp: bool) -> tuple[str, str]:
    kind, label = RESULT_KINDS.get(result, (None, None))
    if kind is None:
        lower = result.lower()
        kind = "mask" if lower.startswith("mask") else "half_map" if "half" in lower else "other"
        label = result.replace("_", " ").strip().capitalize()
    if result == "map" and group_has_sharp:
        label = "Unsharpened map"
    match = CLASS_GROUP.match(group)
    if match:
        label = f"Class {match.group(1)} {label[0].lower()}{label[1:]}"
    return kind, label


def parse_job(doc: dict[str, Any]) -> dict[str, Any]:
    """A job and its map outputs from a CryoSPARC job document (API model dump or ``job.json``)."""

    job_uid = str(doc.get("uid") or "")
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    job_type = str(spec.get("type") or doc.get("job_type") or "")
    volumes_found: list[dict[str, Any]] = []
    order: dict[int, tuple[int, int]] = {}
    resolutions: list[float] = []
    for group_index, group in enumerate(_volume_groups(doc)):
        group_name = str(group["name"] or "volume")
        stats = group["stats"] if isinstance(group["stats"], dict) else {}
        summary = group["summary"] if isinstance(group["summary"], dict) else {}
        resolution = _resolution(stats)
        if resolution is not None:
            resolutions.append(resolution)
        b_factor = _number(stats.get("b_factor"))
        results = [
            result
            for result in group["results"]
            if result["name"] and result["metafiles"] and not result["passthrough"] and result["dtype"] in ("blob", "volume", "")
        ]
        has_sharp = any(result["name"] == "map_sharp" for result in results)
        for result in results:
            name = str(result["name"])
            kind, label = _label(name, group_name, has_sharp)
            metafile = str(result["metafiles"][-1])
            # Results of one group share a grid; older summaries describe only the map.
            shape = summary.get(f"{name}/shape") or summary.get("map/shape")
            shape = [int(n) for n in shape] if isinstance(shape, list) and len(shape) == 3 else None
            psize = _number(summary.get(f"{name}/psize_A")) or _number(summary.get("map/psize_A")) or _number(stats.get("psize"))
            versions = result["versions"]
            order[len(volumes_found)] = (group_index, KIND_ORDER.get(kind, 9))
            volumes_found.append(
                {
                    "job_uid": job_uid,
                    "name": name,
                    "group": group_name,
                    "group_title": group["title"],
                    "result": name,
                    "label": label,
                    "kind": kind,
                    "resolution_A": resolution,
                    "resolution_from": None,
                    "b_factor": b_factor,
                    "psize_A": round(psize, 6) if psize else None,
                    "shape": shape,
                    "size_bytes": 1024 + 4 * shape[0] * shape[1] * shape[2] if shape else None,
                    "path": metafile[:-3] + ".mrc" if metafile.endswith(".cs") else None,
                    "metafile": metafile,
                    "version": versions[-1] if versions else None,
                    "available": True,
                    "stats": None,
                }
            )
    # A result name that appears in several groups (class volumes) is
    # qualified with its group so every volume of a job has a unique name.
    counts: dict[str, int] = {}
    for volume in volumes_found:
        counts[volume["result"]] = counts.get(volume["result"], 0) + 1
    labels: dict[str, int] = {}
    for volume in volumes_found:
        if counts[volume["result"]] > 1:
            volume["name"] = f"{volume['group']}.{volume['result']}"
        labels[volume["label"]] = labels.get(volume["label"], 0) + 1
    for volume in volumes_found:
        if labels[volume["label"]] > 1 and not CLASS_GROUP.match(volume["group"]):
            volume["label"] = f"{volume['label']} ({volume['group_title'] or volume['group']})"
    volumes_found = [volumes_found[index] for index in sorted(order, key=order.get)]
    return {
        "uid": job_uid,
        "project_uid": doc.get("project_uid"),
        "type": job_type,
        "type_title": job_type_title(job_type),
        "title": str(doc.get("title") or ""),
        "status": str(doc.get("status") or ""),
        "created_at": _iso(doc.get("created_at")),
        "completed_at": _iso(doc.get("completed_at")),
        # When it entered each state, and why the scheduler is holding it, for the live status line.
        "queued_at": _iso(doc.get("queued_at")),
        "started_at": _iso(doc.get("started_at")),
        "running_at": _iso(doc.get("running_at")),
        "waiting_at": _iso(doc.get("waiting_at")),
        "queue_message": str(doc.get("queue_message") or "") or None,
        "workspace_uids": [str(uid) for uid in doc.get("workspace_uids") or []],
        "parents": [str(uid) for uid in doc.get("parents") or []],
        "is_final_result": bool(doc.get("is_final_result")),
        "deleted": bool(doc.get("deleted")),
        "resolution_A": min(resolutions) if resolutions else None,
        "resolution_from": None,
        "volumes": volumes_found,
    }


# FSC curves CryoSPARC records: label, curve field, and the prefix of its resolution fields.
FSC_CURVES = (
    ("No mask", "fsc_nomask", "radwn_nomask"),
    ("Spherical mask", "fsc_sphericalmask", "radwn_sphericalmask"),
    ("Loose mask", "fsc_loosemask", "radwn_loosemask"),
    ("Tight mask", "fsc_resmask", "radwn_resmask"),
    ("Auto-tightened mask", "fsc_autotight", "radwn_autotight"),
    ("Auto-tightened, noise substituted", "fsc_autotight_noisesub", "radwn_autotight_noisesub"),
)


def job_details(doc: dict[str, Any]) -> dict[str, Any]:
    """What a run report needs from a job document, beyond ``parse_job``."""

    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    outputs = spec.get("outputs") if isinstance(spec.get("outputs"), dict) else {}
    counts = {name: group.get("num_items") for name, group in outputs.items() if isinstance(group, dict) and group.get("num_items")}
    # Only job.json has the parameters that were set; the API returns every parameter with its value.
    params_spec = doc.get("params_spec") if isinstance(doc.get("params_spec"), dict) else {}
    params = {
        name: entry["value"]
        for name, entry in params_spec.items()
        if isinstance(entry, dict) and "value" in entry and name != "random_seed_default"
    }
    all_params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
    stats: dict[str, Any] = {}
    for group in _volume_groups(doc):
        group_stats = group["stats"] if isinstance(group["stats"], dict) else {}
        if group_stats:
            stats = group_stats
            break
    fsc = stats.get("fsc_info_best") or stats.get("fsc_info")
    fsc = fsc if isinstance(fsc, dict) else {}
    resolutions = []
    for label, _curve, prefix in FSC_CURVES:
        at_143 = _number(fsc.get(f"{prefix}_A"))
        if at_143 is not None and at_143 > 0:
            resolutions.append({"mask": label, "fsc_0143_A": at_143, "fsc_05_A": _number(fsc.get(f"{prefix}_05_A"))})
    instance = doc.get("instance_information") if isinstance(doc.get("instance_information"), dict) else {}
    gpus = instance.get("gpu_info") if isinstance(instance.get("gpu_info"), list) else []
    return {
        "version": str(doc.get("version") or "") or None,
        "params": params,
        "all_params": all_params,
        "counts": counts,
        "fsc": resolutions,
        "fsc_final_A": _number(fsc.get("radwn_final_A")),
        "cfar": _number(stats.get("cfar")),
        "guinier_b_factor": _number(stats.get("guinier_bfactor")),
        "b_factor": _number(stats.get("b_factor")),
        "validation_fsc_A": _number(stats.get("fsc_resolution")),
        "box_size": fsc.get("N"),
        "psize_A": _number(fsc.get("psize")),
        "hostname": instance.get("platform_node"),
        "gpus": sorted({str(gpu.get("name")) for gpu in gpus if isinstance(gpu, dict) and gpu.get("name")}),
    }


def fsc_curves(doc: dict[str, Any]) -> dict[str, Any] | None:
    """A refinement's FSC curves against resolution (1/Å), for plotting."""

    for group in _volume_groups(doc):
        stats = group["stats"] if isinstance(group["stats"], dict) else {}
        fsc = stats.get("fsc_info_best") or stats.get("fsc_info")
        if not isinstance(fsc, dict) or not isinstance(fsc.get("radwns"), list):
            continue
        box = _number(fsc.get("N"))
        psize = _number(fsc.get("psize"))
        if not box or not psize:
            continue
        frequencies = [round(float(radius) / (box * psize), 5) for radius in fsc["radwns"]]
        curves = [
            {"label": label, "values": [round(float(value), 4) for value in fsc[field]]}
            for label, field, _prefix in FSC_CURVES
            if isinstance(fsc.get(field), list) and len(fsc[field]) == len(frequencies)
        ]
        if curves:
            return {"frequency": frequencies, "curves": curves}
    return None


def _uid_number(uid: str) -> int:
    digits = uid[1:] if uid else ""
    return int(digits) if digits.isdigit() else -1


def _inherit_resolution(jobs: list[dict[str, Any]]) -> None:
    """Give post-processing jobs the resolution of the nearest refinement they derive from."""

    parents = {job["uid"]: job["parents"] for job in jobs}
    # Only resolutions a job reported itself count as a source.
    own = {job["uid"]: job["resolution_A"] for job in jobs if job["resolution_A"] is not None}
    for job in jobs:
        if job["resolution_A"] is not None or job["type"] not in INHERITS_RESOLUTION:
            continue
        frontier, seen = list(job["parents"]), {job["uid"]}
        for _ in range(4):
            source = next((uid for uid in frontier if uid in own and uid not in seen), None)
            if source is not None:
                job["resolution_A"] = own[source]
                job["resolution_from"] = source
                break
            seen.update(frontier)
            frontier = [parent for uid in frontier for parent in parents.get(uid, []) if parent not in seen]
            if not frontier:
                break
        for volume in job["volumes"]:
            if volume["resolution_A"] is None and job["resolution_A"] is not None:
                volume["resolution_A"] = job["resolution_A"]
                volume["resolution_from"] = job["resolution_from"]


# ------------------------------------------------------------------ binding
def binding(session: dict[str, Any]) -> Binding | None:
    """The CryoSPARC project a reconstruction session is bound to, or None if not yet."""

    if session.get("kind") != "reconstruction":
        return None
    config = session.get("config") or {}
    manifest = config.get("manifest") or {}
    project_uid = config.get("project_uid")
    workspace_uid = config.get("workspace_uid")
    cryosparc_url = (config.get("agent") or {}).get("cryosparc_url")
    harness_id = session.get("harness_session_id")
    if harness_id and (not project_uid or not cryosparc_url or not manifest):
        try:
            harness = trace_reader.session(harness_id)
        except TraceError:
            harness = {}
        project_uid = project_uid or harness.get("project_uid")
        workspace_uid = workspace_uid or harness.get("workspace_uid")
        cryosparc_url = cryosparc_url or harness.get("cryosparc_url")
        manifest = manifest or harness.get("manifest") or {}
    project_uid = str(project_uid or "").strip()
    if not PROJECT_UID.match(project_uid):
        return None
    project = (manifest.get("project") or {}) if isinstance(manifest, dict) else {}
    return Binding(
        session_id=session["id"],
        project_uid=project_uid,
        workspace_uid=str(workspace_uid).strip() if workspace_uid else None,
        cryosparc_url=str(cryosparc_url).strip() if cryosparc_url else None,
        parent_directory=str(project.get("parent_directory") or "").strip() or None,
        project_title=str(project.get("title") or "").strip() or None,
        harness_session_id=harness_id,
    )


def _require_binding(session: dict[str, Any]) -> Binding:
    bound = binding(session)
    if bound is None:
        raise CryoSPARCError(409, "The reconstruction agent has not created its CryoSPARC project yet.")
    return bound


def _server(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url if "://" in url else f"http://{url}")
    host = (parts.hostname or "").lower()
    local_names = {"localhost", "127.0.0.1", "::1"}
    hostname = socket.gethostname().lower()
    local_names.update({hostname, hostname.split(".")[0]})
    if host in local_names:
        host = "localhost"
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        port = None
    return host, port


def _credentials_for(bound: Binding) -> tuple[str, str, str] | None:
    """Saved CryoSPARC credentials, if they are for the instance this session used."""

    credentials = cryosparc_credentials(required=False)
    if credentials is None:
        return None
    if bound.cryosparc_url and _server(bound.cryosparc_url) != _server(credentials[0]):
        return None
    return credentials


def _sign_in_message(bound: Binding, purpose: str) -> str:
    """What to do when no usable CryoSPARC credentials are saved for this session."""

    if bound.cryosparc_url and cryosparc_credentials(required=False) is not None:
        return f"This session used the CryoSPARC at {bound.cryosparc_url}. Sign in to it in Settings {purpose}."
    return f"Sign in to CryoSPARC in Settings {purpose}."


# -------------------------------------------------------- project directory
_dir_lock = threading.Lock()
_dir_cache: dict[tuple[str, str, str, str], tuple[float, Path | None, str | None]] = {}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _scan_parent(bound: Binding) -> Path | None:
    """Find ``<parent>/CS-*/project.json`` for the session's project uid."""

    if not bound.parent_directory:
        return None
    parent = Path(bound.parent_directory).expanduser()
    try:
        candidates = sorted(parent.glob("CS-*"))
    except OSError:
        return None
    matches: list[tuple[int, Path]] = []
    for candidate in candidates:
        info = _read_json(candidate / "project.json")
        if not info or info.get("uid") != bound.project_uid:
            continue
        # The reconstruction agent writes its session id into the project
        # description, which tells apart two instances' projects with one uid.
        score = 0
        if bound.harness_session_id and bound.harness_session_id in str(info.get("description") or ""):
            score = 2
        elif bound.project_title and bound.project_title == info.get("title"):
            score = 1
        matches.append((score, candidate))
    if not matches:
        return None
    return max(matches, key=lambda match: match[0])[1]


def _project_dir(bound: Binding, credentials: tuple[str, str, str] | None) -> tuple[Path | None, str | None]:
    """(readable local project directory, directory CryoSPARC reports) for a binding."""

    key = (
        _server(credentials[0])[0] if credentials else "",
        bound.project_uid,
        bound.parent_directory or "",
        bound.harness_session_id or "",
    )
    with _dir_lock:
        cached = _dir_cache.get(key)
    if cached is not None:
        checked_at, cached_local, cached_reported = cached
        if cached_local is not None and cached_local.is_dir():
            return cached_local, cached_reported
        if cached_local is None and time.time() - checked_at < MISSING_PROJECT_DIR_SECONDS:
            return None, cached_reported
    local: Path | None = None
    reported: str | None = None
    if credentials is not None:
        try:
            reported = cryosparc_bridge.project_directory(credentials, bound.project_uid)
        except Exception as exc:
            log.info("CryoSPARC did not report the directory of %s: %s", bound.project_uid, _short(exc))
        if reported and Path(reported).is_dir():
            local = Path(reported)
    if local is None:
        local = _scan_parent(bound)
    with _dir_lock:
        _dir_cache[key] = (time.time(), local, reported)
    return local, reported


def project_dir_for_session(session: dict[str, Any]) -> Path | None:
    """The session's CryoSPARC project directory if this server can read it."""

    bound = binding(session)
    if bound is None:
        return None
    local, _reported = _project_dir(bound, _credentials_for(bound))
    return local


def _confined(base: Path, relative: str) -> Path | None:
    relative = str(relative or "").lstrip(">")
    if not relative:
        return None
    root = base.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


# --------------------------------------------------------------- job files
_parse_lock = threading.Lock()
_parsed_jobs: dict[str, tuple[int, int, dict[str, Any]]] = {}
_cs_paths: dict[str, tuple[int, int, str | None]] = {}


def _parsed_job_file(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    with _parse_lock:
        cached = _parsed_jobs.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return _copy_job(cached[2])
    doc = _read_json(path)
    if doc is None or not JOB_UID.match(str(doc.get("uid") or "")):
        return None
    parsed = parse_job(doc)
    with _parse_lock:
        _parsed_jobs[key] = (stat.st_mtime_ns, stat.st_size, parsed)
    return _copy_job(parsed)


def _copy_job(job: dict[str, Any]) -> dict[str, Any]:
    copy = dict(job)
    copy["volumes"] = [dict(volume) for volume in job["volumes"]]
    return copy


def _jobs_from_disk(project_dir: Path, workspace_uid: str | None) -> list[dict[str, Any]]:
    jobs = []
    try:
        entries = [entry for entry in project_dir.iterdir() if JOB_UID.match(entry.name)]
    except OSError as exc:
        raise CryoSPARCError(502, f"Could not read the CryoSPARC project folder: {exc}")
    for entry in entries:
        job = _parsed_job_file(entry / "job.json")
        if job is None or job["deleted"]:
            continue
        if workspace_uid and job["workspace_uids"] and workspace_uid not in job["workspace_uids"]:
            continue
        jobs.append(job)
    return jobs


def _cs_value(data: Any, result: str) -> str | None:
    names = getattr(getattr(data, "dtype", None), "names", None)
    if names is None and hasattr(data, "fields"):
        names = list(data.fields())
    names = list(names or [])
    field = f"{result}/path" if f"{result}/path" in names else next((name for name in names if name.endswith("/path")), None)
    if field is None or len(data[field]) == 0:
        return None
    value = data[field][0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip() or None


def _read_cs_path(cs_file: Path, result: str) -> str | None:
    """The map path recorded in a ``.cs`` metafile, cached by modification time."""

    try:
        stat = cs_file.stat()
    except OSError:
        return None
    key = f"{cs_file}|{result}"
    with _parse_lock:
        cached = _cs_paths.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    import numpy as np

    value: str | None = None
    try:
        value = _cs_value(np.load(str(cs_file), allow_pickle=False), result)
    except Exception:
        try:
            from cryosparc.dataset import Dataset

            value = _cs_value(Dataset.load(str(cs_file)), result)
        except Exception as exc:
            log.info("Could not read %s: %s", cs_file, exc)
    with _parse_lock:
        _cs_paths[key] = (stat.st_mtime_ns, stat.st_size, value)
    return value


def _local_volume_file(project_dir: Path, volume: dict[str, Any]) -> Path | None:
    cs_file = _confined(project_dir, volume["metafile"])
    relative = _read_cs_path(cs_file, volume["result"]) if cs_file is not None else None
    candidate = _confined(project_dir, relative or volume["path"] or "")
    return candidate if candidate is not None and candidate.is_file() else None


def _annotate(volume: dict[str, Any], project_dir: Path | None) -> None:
    """Fill in the real path, size, and cached stats when the project folder is readable."""

    if project_dir is None:
        return
    file = _local_volume_file(project_dir, volume)
    if file is None:
        volume["available"] = False
        return
    try:
        volume["size_bytes"] = file.stat().st_size
    except OSError:
        pass
    try:
        volume["path"] = file.relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        pass
    volume["stats"] = volumes.cached_stats(file)


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    message = next((line.strip() for line in text if line.strip() and not line.startswith("***")), "")
    if not message and text:
        message = re.sub(r"^\*\*\* \[API\] \([^)]*\)\s*", "", text[0]).strip()
    message = message or exc.__class__.__name__
    return message if len(message) <= 200 else message[:197] + "..."


# -------------------------------------------------------------------- jobs
def list_jobs(session: dict[str, Any]) -> dict[str, Any]:
    """Jobs of the session's workspace, newest first, with their maps."""

    bound = binding(session)
    if bound is None:
        return {
            "project_uid": None,
            "workspace_uid": None,
            "project_dir": None,
            "source": None,
            "cryosparc_url": None,
            "jobs": [],
            "reason": "The reconstruction agent has not created its CryoSPARC project yet.",
        }
    credentials = _credentials_for(bound)
    local, reported = _project_dir(bound, credentials)
    jobs: list[dict[str, Any]] | None = None
    source = None
    warning = None
    if credentials is not None:
        try:
            docs = cryosparc_bridge.job_documents(credentials, bound.project_uid, bound.workspace_uid)
            jobs = [parse_job(doc) for doc in docs if not doc.get("deleted")]
            source = "api"
        except Exception as exc:
            log.warning("CryoSPARC job listing failed for %s: %s", bound.project_uid, exc)
            if local is None:
                raise CryoSPARCError(502, f"CryoSPARC did not list the jobs of {bound.project_uid}: {_short(exc)}")
            warning = f"CryoSPARC did not answer ({_short(exc)}), so these jobs are read from the project folder."
    if jobs is None:
        if local is None:
            raise CryoSPARCError(
                400,
                _sign_in_message(
                    bound, f"to see the jobs of {bound.project_uid}; its project folder is not readable from this computer"
                ),
                setup="cryosparc",
            )
        jobs = _jobs_from_disk(local, bound.workspace_uid)
        source = "filesystem"
    _inherit_resolution(jobs)
    for job in jobs:
        job.pop("deleted", None)
        for volume in job["volumes"]:
            _annotate(volume, local)
    jobs.sort(key=lambda job: _uid_number(job["uid"]), reverse=True)
    payload = {
        "project_uid": bound.project_uid,
        "workspace_uid": bound.workspace_uid,
        "project_dir": str(local) if local is not None else reported,
        "project_dir_readable": local is not None,
        "source": source,
        "cryosparc_url": credentials[0] if credentials else bound.cryosparc_url,
        "jobs": jobs,
    }
    if warning:
        payload["warning"] = warning
    return payload


def report_jobs(session: dict[str, Any]) -> dict[str, Any]:
    """Jobs of the session's workspace for a run report, oldest first, each with its ``job_details``.

    The project folder comes first because only job.json records which
    parameters were set. Without it, the jobs come from the CryoSPARC API.
    """

    bound = _require_binding(session)
    credentials = _credentials_for(bound)
    local, reported = _project_dir(bound, credentials)
    docs: list[dict[str, Any]] = []
    source = None
    if local is not None:
        for entry in local.iterdir():
            if JOB_UID.match(entry.name):
                doc = _read_json(entry / "job.json")
                if doc is not None and JOB_UID.match(str(doc.get("uid") or "")):
                    docs.append(doc)
        source = "filesystem"
    elif credentials is not None:
        try:
            docs = cryosparc_bridge.job_documents(credentials, bound.project_uid, bound.workspace_uid)
        except Exception as exc:
            raise CryoSPARCError(502, f"CryoSPARC did not list the jobs of {bound.project_uid}: {_short(exc)}")
        source = "api"
    else:
        raise CryoSPARCError(
            400,
            _sign_in_message(bound, f"to read the jobs of {bound.project_uid}; its project folder is not readable from this computer"),
            setup="cryosparc",
        )
    documents = {}
    jobs = []
    for doc in docs:
        job = parse_job(doc)
        if job["deleted"]:
            continue
        if source == "filesystem" and bound.workspace_uid and job["workspace_uids"] and bound.workspace_uid not in job["workspace_uids"]:
            continue
        job.update(job_details(doc))
        documents[job["uid"]] = doc
        jobs.append(job)
    _inherit_resolution(jobs)
    for job in jobs:
        for volume in job["volumes"]:
            _annotate(volume, local)
    jobs.sort(key=lambda job: _uid_number(job["uid"]))
    return {
        "project_uid": bound.project_uid,
        "workspace_uid": bound.workspace_uid,
        "project_dir": str(local) if local is not None else reported,
        "source": source,
        "jobs": jobs,
        "documents": documents,
    }


def _job(bound: Binding, credentials: tuple[str, str, str] | None, local: Path | None, job_uid: str) -> dict[str, Any]:
    api_error: Exception | None = None
    if credentials is not None:
        try:
            return parse_job(cryosparc_bridge.job_document(credentials, bound.project_uid, job_uid))
        except Exception as exc:
            api_error = exc
    if local is not None:
        job = _parsed_job_file(local / job_uid / "job.json")
        if job is not None and not job["deleted"]:
            return job
    if api_error is not None and getattr(api_error, "code", None) != 404:
        raise CryoSPARCError(502, f"CryoSPARC did not return {job_uid}: {_short(api_error)}")
    if credentials is None and local is None:
        raise CryoSPARCError(400, _sign_in_message(bound, f"to open the maps of {bound.project_uid}"), setup="cryosparc")
    raise CryoSPARCError(404, f"Job {job_uid} was not found in {bound.project_uid}.")


def _match_volume(job: dict[str, Any], name: str) -> dict[str, Any] | None:
    for volume in job["volumes"]:
        if name in (volume["name"], f"{volume['group']}.{volume['result']}"):
            return volume
    return None


def volume_filename(project_uid: str, job_uid: str, name: str, suffix: str = "") -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "map"
    return f"{project_uid}_{job_uid}_{safe}{suffix}.mrc"


_download_locks_guard = threading.Lock()
_download_locks: dict[str, threading.Lock] = {}
_remote_paths: dict[str, str | None] = {}


def _lock_for(key: str) -> threading.Lock:
    with _download_locks_guard:
        lock = _download_locks.get(key)
        if lock is None:
            lock = _download_locks[key] = threading.Lock()
        return lock


def _url_slug(url: str) -> str:
    host, port = _server(url)
    return f"{re.sub(r'[^A-Za-z0-9.-]+', '_', host)}_{port}_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:8]}"


def _safe_relative(path: str | None) -> PurePosixPath | None:
    if not path:
        return None
    relative = PurePosixPath(path.lstrip(">"))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    return relative


def _download_volume(credentials: tuple[str, str, str], bound: Binding, job: dict[str, Any], volume: dict[str, Any]) -> Path:
    stamp = re.sub(r"[^0-9A-Za-z]", "", job.get("completed_at") or "") or "pending"
    base = CACHE_DIR / "maps" / _url_slug(credentials[0]) / bound.project_uid / job["uid"] / stamp
    key = f"{credentials[0]}|{bound.project_uid}|{volume['metafile']}|{volume['result']}|{stamp}"
    remote = _remote_paths.get(key)
    if remote is None:
        try:
            value = cryosparc_bridge.dataset_field(credentials, bound.project_uid, volume["metafile"], f"{volume['result']}/path")
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            remote = _remote_paths[key] = str(value).strip() or None
        except Exception as exc:
            # The map usually sits next to its metafile under the same name.
            log.info("Could not read %s through CryoSPARC: %s", volume["metafile"], _short(exc))
    relative = _safe_relative(remote) or _safe_relative(volume["path"])
    if relative is None:
        raise CryoSPARCError(404, f"CryoSPARC has no file for {job['uid']} {volume['label'].lower()}.")
    target = base / relative.name
    with _lock_for(str(target)):
        if target.is_file():
            _touch(target)
            return target
        partial = target.with_name(f".{target.name}.part")
        try:
            cryosparc_bridge.download_project_file(credentials, bound.project_uid, relative.as_posix(), partial)
            os.replace(partial, target)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise CryoSPARCError(502, f"Could not download {relative.name} from CryoSPARC: {_short(exc)}")
    _prune(CACHE_DIR / "maps", MAP_CACHE_BYTES, keep={target})
    return target


def volume_file(session: dict[str, Any], job_uid: str, name: str) -> tuple[Path, dict[str, Any], dict[str, Any], Binding]:
    """(full-resolution map file, volume, job, binding) for one map of a session's job."""

    job_uid = str(job_uid or "").strip()
    name = str(name or "").strip()
    if not JOB_UID.match(job_uid):
        raise CryoSPARCError(400, f"{job_uid or 'An empty value'} is not a CryoSPARC job id.")
    if not name:
        raise CryoSPARCError(400, "Name the map to open.")
    bound = _require_binding(session)
    credentials = _credentials_for(bound)
    local, _reported = _project_dir(bound, credentials)
    job = _job(bound, credentials, local, job_uid)
    volume = _match_volume(job, name)
    if volume is None:
        raise CryoSPARCError(404, f"{job_uid} has no map named {name}.")
    if local is not None:
        # The project folder is CryoSPARC's own, so a file missing here is gone.
        file = _local_volume_file(local, volume)
        if file is None:
            raise CryoSPARCError(404, f"The file for {job_uid} {volume['label'].lower()} is no longer in the project folder.")
        _annotate(volume, local)
        return file, volume, job, bound
    # _job() has already refused when there are neither credentials nor a folder.
    return _download_volume(credentials, bound, job, volume), volume, job, bound


def describe_volume(session: dict[str, Any], job_uid: str, name: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(volume, job) from the session's job listing, with resolution inherited from parents."""

    try:
        listing = list_jobs(session)
    except CryoSPARCError:
        return None
    for job in listing["jobs"]:
        if job["uid"] == job_uid:
            volume = _match_volume(job, name)
            return (volume, job) if volume is not None else None
    return None


# ------------------------------------------------------------- project maps
def project_maps(project: dict[str, Any], include_all: bool = False) -> dict[str, Any]:
    """Maps of completed jobs from every reconstruction session of a project, newest first.

    By default only full, sharpened, and locally filtered maps are listed;
    ``include_all`` adds half maps, masks, and other volumes. A session that
    cannot be read is reported in ``errors`` and skipped.
    """

    maps: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for session in store.list_sessions(project["id"], "reconstruction"):
        if binding(session) is None:
            continue
        try:
            listing = list_jobs(session)
        except Exception as exc:
            message = exc.message if isinstance(exc, CryoSPARCError) else _short(exc)
            log.warning("Could not list the maps of session %s: %s", session["id"], message)
            error = {"session_id": session["id"], "session_title": session.get("title"), "message": message}
            # "cryosparc" when signing in would help; the attach menu then offers Settings.
            setup = exc.extra.get("setup") if isinstance(exc, CryoSPARCError) else None
            if setup:
                error["setup"] = setup
            errors.append(error)
            continue
        for job in listing["jobs"]:
            if job["status"] != "completed":
                continue
            for volume in job["volumes"]:
                if not include_all and volume["kind"] not in MODELING_KINDS:
                    continue
                maps.append(
                    {
                        **volume,
                        "project_uid": listing["project_uid"],
                        "session_id": session["id"],
                        "session_title": session.get("title"),
                        "job_type": job["type"],
                        "job_type_title": job["type_title"],
                        "job_title": job["title"],
                        "job_status": job["status"],
                        "completed_at": job["completed_at"],
                        "is_final_result": job["is_final_result"],
                        "source": listing["source"],
                    }
                )
    maps.sort(
        key=lambda item: (item["completed_at"] or "", _uid_number(item["job_uid"]), -KIND_ORDER.get(item["kind"], 9)),
        reverse=True,
    )
    return {"maps": maps, "errors": errors}


# ------------------------------------------------------------------ assets
_asset_lock = threading.Lock()
_asset_index: dict[tuple[str, str, str], tuple[float, dict[str, dict[str, Any]]]] = {}


def _image_format(asset: dict[str, Any]) -> str | None:
    fmt = IMAGE_FORMATS.get(asset.get("content_type") or "")
    if fmt is None:
        fmt = IMAGE_EXTENSIONS.get(Path(asset.get("filename") or "").suffix.lower())
    return fmt


def _fetch_assets(credentials: tuple[str, str, str], bound: Binding, job_uid: str) -> dict[str, dict[str, Any]]:
    raw = cryosparc_bridge.list_assets(credentials, bound.project_uid, job_uid)
    assets: dict[str, dict[str, Any]] = {}
    for asset in raw:
        fmt = _image_format(asset)
        if fmt is None or not ASSET_ID.match(asset.get("id") or ""):
            continue
        assets[asset["id"]] = {
            "id": asset["id"],
            "filename": asset.get("filename") or f"{asset['id']}.{fmt}",
            "kind": "image",
            "format": fmt,
            "content_type": MEDIA_TYPES[fmt],
            "size": asset.get("size") or 0,
            "uploaded_at": _iso(asset.get("uploaded_at")),
        }
    with _asset_lock:
        _asset_index[(credentials[0], bound.project_uid, job_uid)] = (time.time(), assets)
    return assets


def list_assets(session: dict[str, Any], job_uid: str) -> dict[str, Any]:
    """Images CryoSPARC stored for a job (FSC curves, class averages), newest first."""

    job_uid = str(job_uid or "").strip()
    if not JOB_UID.match(job_uid):
        raise CryoSPARCError(400, f"{job_uid or 'An empty value'} is not a CryoSPARC job id.")
    bound = binding(session)
    if bound is None:
        return {"assets": [], "reason": "The reconstruction agent has not created its CryoSPARC project yet."}
    credentials = _credentials_for(bound)
    if credentials is None:
        return {"assets": [], "reason": _sign_in_message(bound, "to see this job's images")}
    try:
        assets = list(_fetch_assets(credentials, bound, job_uid).values())
    except Exception as exc:
        log.warning("CryoSPARC asset listing failed for %s %s: %s", bound.project_uid, job_uid, exc)
        return {"assets": [], "reason": f"CryoSPARC did not return this job's images: {_short(exc)}"}
    assets.sort(key=lambda asset: (asset["uploaded_at"] or "", asset["filename"]), reverse=True)
    payload: dict[str, Any] = {"assets": assets[:MAX_ASSETS]}
    if len(assets) > MAX_ASSETS:
        payload["truncated"] = True
    return payload


def asset_file(session: dict[str, Any], job_uid: str, asset_id: str) -> tuple[Path, str, str]:
    """(cached file, media type, filename) for one image asset of a session's job."""

    job_uid = str(job_uid or "").strip()
    asset_id = str(asset_id or "").strip().lower()
    if not JOB_UID.match(job_uid):
        raise CryoSPARCError(400, f"{job_uid or 'An empty value'} is not a CryoSPARC job id.")
    if not ASSET_ID.match(asset_id):
        raise CryoSPARCError(400, "That is not a CryoSPARC asset id.")
    bound = _require_binding(session)
    credentials = _credentials_for(bound)
    if credentials is None:
        raise CryoSPARCError(400, _sign_in_message(bound, "to see this job's images"), setup="cryosparc")
    with _asset_lock:
        cached = _asset_index.get((credentials[0], bound.project_uid, job_uid))
    assets = cached[1] if cached and time.time() - cached[0] < ASSET_INDEX_SECONDS else None
    if assets is None or asset_id not in assets:
        try:
            assets = _fetch_assets(credentials, bound, job_uid)
        except Exception as exc:
            raise CryoSPARCError(502, f"CryoSPARC did not return this job's images: {_short(exc)}")
    asset = assets.get(asset_id)
    if asset is None:
        raise CryoSPARCError(404, f"{job_uid} has no image with that id.")
    directory = CACHE_DIR / "assets"
    target = directory / f"{asset_id}.{asset['format']}"
    with _lock_for(str(target)):
        if not target.is_file():
            partial = target.with_name(f".{target.name}.part")
            try:
                cryosparc_bridge.download_asset(credentials, asset_id, partial)
                os.replace(partial, target)
            except Exception as exc:
                partial.unlink(missing_ok=True)
                raise CryoSPARCError(502, f"Could not download {asset['filename']} from CryoSPARC: {_short(exc)}")
            _prune(directory, ASSET_CACHE_BYTES, keep={target})
        else:
            _touch(target)
    return target, asset["content_type"], asset["filename"]


# ------------------------------------------------------------------- cache
def _touch(path: Path) -> None:
    try:
        os.utime(path)
    except OSError:
        pass


def _prune(directory: Path, max_bytes: int, keep: set[Path]) -> None:
    """Delete the least recently used cached files until ``directory`` fits ``max_bytes``."""

    entries = []
    for dirpath, _dirnames, filenames in os.walk(directory):
        for filename in filenames:
            if filename.endswith(".part"):
                continue
            path = Path(dirpath) / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
    used = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if used <= max_bytes:
            break
        if path in keep:
            continue
        try:
            path.unlink()
            used -= size
        except OSError:
            continue
