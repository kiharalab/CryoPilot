"""The run report and the transcript export of a session.

A report states what a session produced and how, from the records CryoSPARC and
the agents keep: the final map or model, its FSC or validation numbers, the jobs
and parameters, the software versions, the cost, and a draft Methods paragraph.
Everything comes from those records and no model is called, so a report is free,
gives the same text each time, and contains nothing that did not happen.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..version import VERSION
from . import cryosparc_jobs, files as file_roots, guides, modeling_toolchain
from .config import MODELING_SANDBOX_ROOT
from .cryosparc_jobs import CryoSPARCError, job_type_title
from .state import processes, store, trace_reader, usage_view
from .trace import COMPACTION_PREFIX, TraceError

REFINEMENT_TYPES = {
    "homo_refine_new",
    "homo_refine",
    "nonuniform_refine_new",
    "nonuniform_refine",
    "new_local_refine",
    "local_refine",
    "homo_reconstruct",
    "flex_highres",
}
POST_PROCESSING_TYPES = {"validation", "sharpen", "local_resolution", "local_filter", "orientation_diagnostics", "volume_tools"}
MODEL_FILE = re.compile(r"[\w./\-]+\.(?:cif|mmcif|pdb)\b", re.IGNORECASE)
PARAMETERS_LIMIT = 240


def _table(header: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows]
    return "\n".join(lines)


def _when(value: Any) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _span(start: Any, end: Any) -> str:
    try:
        begin = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return ""
    total = int((finish - begin).total_seconds())
    if total < 0:
        return ""
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} min"
    return f"{total // 3600} h {total % 3600 // 60:02d} min"


def _list(items: list[str]) -> str:
    """Items in a sentence, with an Oxford comma."""

    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _capitalized(sentence: str) -> str:
    return sentence[:1].upper() + sentence[1:]


def _plain_number(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


# ------------------------------------------------------------------- shared
def _usage_section(session: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    usage = usage_view(session)
    lines = ["## Cost", ""]
    if not usage["requests"]:
        lines.append("This session has no recorded usage. It ran before CryoPilot recorded model usage, or it has made no model request yet.")
        return lines, usage
    rows = [
        [
            row["model"],
            f"{row['requests']:,}",
            f"{row['input_tokens']:,}",
            f"{row['cached_tokens']:,}",
            f"{row['cache_write_tokens']:,}",
            f"{row['output_tokens']:,}",
            "not known" if row["unpriced_requests"] else f"${row['cost_usd']:.2f}",
        ]
        for row in usage["models"]
    ]
    lines.append(_table(["Model", "Requests", "Input tokens", "Cache reads", "Cache writes", "Output tokens", "Estimated cost"], rows))
    lines += ["", f"Estimated total: **${usage['cost_usd']:.2f}**, from list prices as of {usage['prices_as_of']}."]
    if usage["unpriced_models"]:
        lines.append(f"The total leaves out {_list(usage['unpriced_models'])}, which have no listed price in this version of CryoPilot.")
    return lines, usage


def _header(session: dict[str, Any], kind_label: str) -> list[str]:
    project = store.get_project(session["project_id"])
    lines = [f"# Run report: {session['title']}", "", f"{kind_label} session in project {project['title']}."]
    if processes.is_alive(session["id"]) and processes.status(session["id"])["state"] in {"starting", "connecting", "initializing", "running", "paused"}:
        lines += ["", "**This run is still going, so this report describes it as it stands now.**"]
    return lines


def _result(session: dict[str, Any], markdown: list[str], methods: str, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": session["id"],
        "kind": session["kind"],
        "title": session["title"],
        "filename": f"{_slug(session['title'])}-report.md",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markdown": "\n".join(markdown).strip() + "\n",
        "methods": methods,
        **extra,
    }


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "session"


def _cryosparc_tools_version() -> str | None:
    try:
        return importlib.metadata.version("cryosparc-tools")
    except importlib.metadata.PackageNotFoundError:
        return None


# ------------------------------------------------------------- reconstruction
def choose_final_job(jobs: list[dict[str, Any]], last_message: str) -> tuple[dict[str, Any] | None, str]:
    """The refinement this session's result came from, and why it was chosen."""

    by_uid = {job["uid"]: job for job in jobs}
    # A validation or sharpening job reports a resolution too, but its map is the refinement's.
    candidates = [
        job
        for job in jobs
        if job["status"] == "completed"
        and job["resolution_A"] is not None
        and not job["resolution_from"]
        and job["volumes"]
        and job["type"] not in POST_PROCESSING_TYPES
    ]
    if not candidates:
        return None, ""
    flagged = [job for job in candidates if job["is_final_result"]]
    if flagged:
        return flagged[-1], "it is marked as the final result in CryoSPARC"
    named = []
    for uid in dict.fromkeys(re.findall(r"\bJ\d+\b", last_message or "")):
        job = by_uid.get(uid)
        if job is None:
            continue
        # A sharpened, filtered, or validated map stands for the refinement it came from.
        sources = [by_uid.get(job["resolution_from"])] if job["resolution_from"] else [job, *(by_uid.get(uid) for uid in job["parents"])]
        source = next((entry for entry in sources if entry in candidates), None)
        if source is not None and source not in named:
            named.append(source)
    if named:
        return min(named, key=lambda job: job["resolution_A"]), "the agent's last message names it and it has the best resolution of the jobs named there"
    return min(candidates, key=lambda job: job["resolution_A"]), "it has the best resolution in this workspace"


def _ancestors(jobs: list[dict[str, Any]], uid: str) -> set[str]:
    parents = {job["uid"]: job["parents"] for job in jobs}
    seen: set[str] = set()
    stack = list(parents.get(uid, []))
    while stack:
        current = stack.pop()
        if current in seen or current not in parents:
            continue
        seen.add(current)
        stack.extend(parents[current])
    return seen


def _post_processing(jobs: list[dict[str, Any]], uid: str) -> list[dict[str, Any]]:
    return [
        job
        for job in jobs
        if job["status"] == "completed" and job["type"] in POST_PROCESSING_TYPES and uid in _ancestors(jobs, job["uid"])
    ]


def _particle_count(job: dict[str, Any]) -> int | None:
    return job["counts"].get("particles")


def _parameters(job: dict[str, Any]) -> str:
    text = ", ".join(f"{name}={_plain_number(value)}" for name, value in job["params"].items())
    return text[:PARAMETERS_LIMIT] + "…" if len(text) > PARAMETERS_LIMIT else text


def _job_rows(jobs: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            job["uid"],
            job["type_title"],
            job["status"],
            _span(job["started_at"], job["completed_at"]),
            f"{_particle_count(job):,}" if _particle_count(job) else "",
            _parameters(job),
        ]
        for job in jobs
    ]


def reconstruction_methods(
    final: dict[str, Any] | None,
    chain: list[dict[str, Any]],
    post: list[dict[str, Any]],
    manifest: dict[str, Any],
    agent: dict[str, Any],
    version: str | None,
) -> str:
    """A draft Methods paragraph describing the jobs that led to the final map."""

    sentences = []
    software = f"CryoSPARC {version}" if version else "CryoSPARC"
    model = agent.get("model")
    used = f" using {model}" + (f" at {agent['effort']} reasoning effort" if agent.get("effort") else "") if model else ""
    sentences.append(f"Data were processed in {software} by the reconstruction agent of CryoPilot {VERSION}{used}.")

    acquisition = (manifest or {}).get("acquisition") or {}
    recorded = []
    if acquisition.get("pixel_size_A"):
        recorded.append(f"a pixel size of {_plain_number(acquisition['pixel_size_A'])} Å")
    if acquisition.get("accelerating_voltage_kV"):
        recorded.append(f"an accelerating voltage of {_plain_number(acquisition['accelerating_voltage_kV'])} kV")
    if acquisition.get("spherical_aberration_mm") is not None:
        recorded.append(f"a spherical aberration of {_plain_number(acquisition['spherical_aberration_mm'])} mm")
    if acquisition.get("total_dose_e_per_A2"):
        recorded.append(f"a total dose of {_plain_number(acquisition['total_dose_e_per_A2'])} e⁻/Å²")
    if recorded:
        sentences.append(f"The data had {_list(recorded)}.")

    def of_type(*types: str) -> list[dict[str, Any]]:
        return [job for job in chain if job["type"] in types]

    def named(jobs: list[dict[str, Any]]) -> str:
        titles: dict[str, list[str]] = {}
        for job in jobs:
            titles.setdefault(job["type_title"], []).append(job["uid"])
        return _list([f"{title} ({', '.join(uids)})" for title, uids in titles.items()])

    imports = of_type("import_movies", "import_micrographs", "import_particles")
    if imports:
        first = imports[0]
        total = max(first["counts"].values(), default=None)
        sentences.append(f"{first['type_title']} ({first['uid']}) brought in " + (f"{total:,} items." if total else "the data."))
    preprocess = of_type("patch_motion_correction_multi", "patch_ctf_estimation_multi")
    if preprocess:
        sentences.append(f"Exposures were prepared with {named(preprocess)}.")
    for job in of_type("curate_exposures_v2"):
        accepted = job["counts"].get("exposures_accepted")
        rejected = job["counts"].get("exposures_rejected") or 0
        if accepted:
            sentences.append(f"Curation ({job['uid']}) kept {accepted:,} of {accepted + rejected:,} exposures.")
    pickers = of_type("blob_picker_gpu", "template_picker_gpu", "topaz_extract", "manual_picker", "filament_tracer_gpu")
    if pickers:
        sentences.append(f"Particles were picked with {named(pickers)}.")
    extractions = of_type("extract_micrographs_multi", "extract_micrographs_cpu_parallel", "downsample_particles", "restack_particles")
    if extractions:
        last = extractions[-1]
        box = last["params"].get("box_size_pix")
        binned = last["params"].get("bin_size_pix")
        details = f" with a box of {box} pixels" if box else ""
        if box and binned:
            details += f" binned to {binned} pixels"
        count = f", which gave {_particle_count(last):,} particles" if _particle_count(last) else ""
        sentences.append(f"Particles were extracted{details} ({last['uid']}){count}.")
    sorting = of_type("class_2D_new", "class_2D", "select_2D", "homo_abinit", "hetero_refine", "class_3D")
    if sorting:
        sentences.append(f"Particles were sorted with {named(sorting)}.")
    corrections = of_type("ctf_refine_global", "ctf_refine_local", "reference_motion_correction")
    if corrections:
        sentences.append(f"Particle parameters were improved with {named(corrections)}.")
    earlier = [job for job in chain if job["type"] in REFINEMENT_TYPES]
    if earlier:
        sentences.append(f"Earlier refinements were {named(earlier)}.")
    if final:
        details = []
        symmetry = final["all_params"].get("refine_symmetry") or final["params"].get("refine_symmetry")
        if symmetry:
            details.append(f"{symmetry} symmetry")
        if _particle_count(final):
            details.append(f"{_particle_count(final):,} particles")
        resolution = final["fsc_final_A"] or final["resolution_A"]
        result = f"{final['type_title']} ({final['uid']})" + (f" with {_list(details)}" if details else "")
        sentence = f"{result} gave a map at {resolution:.2f} Å by the gold-standard FSC at 0.143"
        if final["box_size"] and final["psize_A"]:
            sentence += f", with a box of {final['box_size']} pixels at {final['psize_A']:.3f} Å per pixel"
        if final["cfar"] is not None:
            sentence += f" and a cFAR of {final['cfar']:.2f}"
        sentences.append(f"{sentence}.")
    afterwards = []
    for job in post:
        if job["type"] == "sharpen":
            afterwards.append(f"the map was sharpened ({job['uid']}" + (f", B-factor {job['b_factor']:.1f} Å²)" if job["b_factor"] is not None else ")"))
        elif job["type"] == "local_resolution":
            afterwards.append(f"local resolution was estimated ({job['uid']})")
        elif job["type"] == "local_filter":
            afterwards.append(f"the map was locally filtered ({job['uid']})")
        elif job["type"] == "validation":
            value = f", {job['validation_fsc_A']:.2f} Å" if job["validation_fsc_A"] else ""
            afterwards.append(f"the FSC was validated ({job['uid']}{value})")
        elif job["type"] == "orientation_diagnostics":
            afterwards.append(f"orientation distribution was checked ({job['uid']})")
    if afterwards:
        sentences.append(_capitalized(f"{_list(afterwards)}."))
    return " ".join(sentences)


def _reconstruction_report(session: dict[str, Any]) -> dict[str, Any]:
    config = session.get("config") or {}
    harness: dict[str, Any] = {}
    last_message = ""
    harness_id = session.get("harness_session_id")
    if harness_id:
        try:
            harness = trace_reader.session(harness_id)
            last_message = trace_reader.last_assistant_text(harness_id)
        except TraceError:
            pass
    manifest = config.get("manifest") or harness.get("manifest") or {}
    model = harness.get("model") or (config.get("agent") or {}).get("model")
    effort = (harness.get("reasoning") or {}).get("effort") or (config.get("agent") or {}).get("reasoning_effort")
    usage_lines, usage = _usage_section(session)

    lines = _header(session, "Reconstruction")
    if manifest.get("user_goal"):
        lines += ["", f"**Goal:** {manifest['user_goal']}"]

    jobs: list[dict[str, Any]] = []
    documents: dict[str, Any] = {}
    jobs_error = None
    project_dir = None
    try:
        loaded = cryosparc_jobs.report_jobs(session)
        jobs, documents, project_dir = loaded["jobs"], loaded["documents"], loaded["project_dir"]
    except CryoSPARCError as exc:
        jobs_error = exc.message

    final, reason = choose_final_job(jobs, last_message)
    ancestors = _ancestors(jobs, final["uid"]) if final else set()
    chain = [job for job in jobs if job["uid"] in ancestors]
    post = _post_processing(jobs, final["uid"]) if final else []
    version = next((job["version"] for job in reversed(jobs) if job["version"]), None)

    lines += ["", "## Final map", ""]
    if jobs_error:
        lines.append(f"The jobs could not be read: {jobs_error}")
    elif final is None:
        lines.append("No completed job in this session's workspace has a resolution yet.")
    else:
        resolution = final["fsc_final_A"] or final["resolution_A"]
        lines.append(f"**{final['uid']}, {final['type_title']}: {resolution:.2f} Å.** Chosen because {reason}. Check that this is the map you want.")
        if final["title"]:
            lines += ["", f"CryoSPARC title: {final['title']}"]
        rows = [[volume["label"], _absolute(project_dir, volume["path"])] for volume in final["volumes"] if volume["kind"] in ("map", "map_sharp", "half_map", "mask")]
        for job in post:
            rows += [[f"{volume['label']} ({job['uid']})", _absolute(project_dir, volume["path"])] for volume in job["volumes"] if volume["kind"] in ("map", "map_sharp")]
        if rows:
            lines += ["", _table(["File", "Path"], rows)]

        lines += ["", "## FSC", ""]
        if final["fsc"]:
            lines.append(
                _table(
                    ["Mask", "Resolution at FSC 0.143 (Å)", "Resolution at FSC 0.5 (Å)"],
                    [[row["mask"], f"{row['fsc_0143_A']:.2f}", f"{row['fsc_05_A']:.2f}" if row["fsc_05_A"] else ""] for row in final["fsc"]],
                )
            )
        else:
            lines.append(f"{resolution:.2f} Å. This job records no separate curves for each mask.")
        facts = []
        if _particle_count(final):
            facts.append(f"{_particle_count(final):,} particles")
        if final["box_size"] and final["psize_A"]:
            facts.append(f"a box of {final['box_size']} pixels at {final['psize_A']:.3f} Å per pixel")
        if final["cfar"] is not None:
            facts.append(f"a cFAR of {final['cfar']:.2f}")
        if final["guinier_b_factor"] is not None:
            facts.append(f"a Guinier B-factor of {final['guinier_b_factor']:.1f} Å²")
        if facts:
            lines += ["", f"This map has {_list(facts)}."]
        for job in post:
            if job["validation_fsc_A"]:
                lines += ["", f"Validation job {job['uid']}: {job['validation_fsc_A']:.2f} Å."]

    methods = reconstruction_methods(final, chain, post, manifest, {"model": model, "effort": effort}, version) if jobs else ""
    if methods:
        lines += ["", "## Draft Methods", "", methods]

    if jobs:
        lines += ["", "## Jobs", ""]
        used = [*chain, *([final] if final else []), *post]
        used_uids = {job["uid"] for job in used}
        others = [job for job in jobs if job["uid"] not in used_uids]
        if final:
            lines += ["Jobs that led to the final map, with the parameters that were set:", "", _table(["Job", "Type", "Status", "Run time", "Particles", "Parameters"], _job_rows(used))]
        if others:
            lines += ["", "Other jobs in this workspace:", "", _table(["Job", "Type", "Status", "Run time", "Particles", "Parameters"], _job_rows(others))]
        if not any(job["params"] for job in jobs):
            lines += ["", "The parameters are recorded in the project folder, which this computer could not read."]

    gpus = sorted({gpu for job in jobs for gpu in job["gpus"]})
    software_rows = [["CryoPilot", VERSION], ["CryoSPARC", version or "not recorded"]]
    tools_version = _cryosparc_tools_version()
    if tools_version:
        software_rows.append(["cryosparc-tools", tools_version])
    software_rows.append(["Model", f"{model or 'not recorded'}" + (f", {effort} reasoning effort" if effort else "")])
    guide = config.get("processing_guide")
    if guide:
        software_rows.append(["Processing guide", guides.GUIDE_LABELS.get(guide, guide)])
    if config.get("experimental"):
        software_rows.append(["Experimental features", "web search and page reading"])
    if gpus:
        software_rows.append(["GPU", ", ".join(gpus)])
    lines += ["", "## Software", "", _table(["Item", "Version"], software_rows)]
    if harness.get("project_uid"):
        lines += ["", f"CryoSPARC project {harness['project_uid']}, workspace {harness.get('workspace_uid') or 'not recorded'}" + (f", folder {project_dir}." if project_dir else ".")]
    if harness.get("created_at"):
        lines += ["", f"Started {_when(harness['created_at'])}; last activity {_when(harness.get('updated_at'))}."]
    lines += ["", *usage_lines]

    curve = None
    if final and final["uid"] in documents:
        curve = cryosparc_jobs.fsc_curves(documents[final["uid"]])
    return _result(
        session,
        lines,
        methods,
        final={"job_uid": final["uid"], "resolution_A": final["fsc_final_A"] or final["resolution_A"], "reason": reason} if final else None,
        fsc_curve=curve,
        usage=usage,
        error=jobs_error,
    )


def _absolute(project_dir: Any, path: Any) -> str:
    if not path:
        return ""
    return str(Path(str(project_dir)) / str(path)) if project_dir and not str(path).startswith("/") else str(path)


# ------------------------------------------------------------------ modeling
def _tool_runs(session_id: str) -> list[dict[str, Any]]:
    """Each tool the modeling agent ran, in order: its arguments and its parsed result."""

    runs: dict[str, dict[str, Any]] = {}
    order = []
    for event in store.list_modeling_events(session_id):
        payload = event["payload"] if isinstance(event["payload"], dict) else {}
        call_id = str(payload.get("call_id") or event["id"])
        if event["kind"] == "tool_call":
            runs[call_id] = {"name": payload.get("name"), "arguments": payload.get("arguments") or {}, "output": None, "text": "", "at": event["created_at"]}
            order.append(call_id)
        elif event["kind"] == "tool_output" and call_id in runs:
            text = str(payload.get("text") or "")
            runs[call_id]["text"] = text
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            runs[call_id]["output"] = parsed if isinstance(parsed, dict) else None
    return [runs[call_id] for call_id in order]


def _resolve_model_file(name: str, workspace: Path) -> Path | None:
    candidate = Path(name)
    options = [candidate] if candidate.is_absolute() else [workspace / candidate, MODELING_SANDBOX_ROOT / candidate]
    for option in options:
        try:
            if option.is_file():
                return option
        except OSError:
            continue
    return None


def _digest(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def choose_final_model(runs: list[dict[str, Any]], last_message: str, workspace: Path) -> tuple[Path | None, str]:
    """The model the session ended with, and why it was chosen."""

    for name in MODEL_FILE.findall(last_message or ""):
        found = _resolve_model_file(name, workspace)
        if found is not None:
            return found, "the agent's last message names it"
    for run in reversed(runs):
        output = run["output"] or {}
        for key in ("refined_model", "output_path", "aligned_model", "model_path"):
            value = output.get(key)
            if isinstance(value, str):
                found = _resolve_model_file(value, workspace)
                if found is not None:
                    return found, "it is the last model a tool wrote"
    return None, ""


def _measurement_for(runs: list[dict[str, Any]], tool: str, final_digest: str | None, workspace: Path) -> tuple[dict[str, Any] | None, bool]:
    """The newest result of a tool for the final model, else its newest result at all (with False)."""

    results = [run for run in runs if run["name"] == tool and run["output"]]
    for run in reversed(results):
        path = run["output"].get("model_path") or run["arguments"].get("model_path")
        found = _resolve_model_file(str(path), workspace) if path else None
        if final_digest and _digest(found) == final_digest:
            return run["output"], True
    return (results[-1]["output"], False) if results else (None, False)


def _software_versions(runs: list[dict[str, Any]]) -> list[list[str]]:
    used = {run["name"] for run in runs}
    rows = []
    commands = " ".join(str((run["output"] or {}).get("command") or "") for run in runs)
    phenix = re.search(r"phenix[-_]([\w.\-]+?)/", commands)
    toolchain = modeling_toolchain.status() or {}

    def path_of(name: str) -> str:
        entry = toolchain.get(name) or {}
        return str(entry.get("path") or "") if entry.get("ok") else ""

    if used & {"refine_phenix", "validate_model_phenix"}:
        rows.append(["Phenix", phenix.group(1) if phenix else path_of("phenix") or "not recorded"])
    if "refine_rosetta" in used:
        rows.append(["Rosetta", path_of("rosetta") or "not recorded"])
    if "align_model_to_map" in used or "compare_map_hands" in used:
        executable = next((str((run["output"] or {}).get("executable")) for run in runs if (run["output"] or {}).get("executable")), "")
        rows.append(["VESPER", "included with CryoPilot" if ".deps" in executable else executable or path_of("vesper") or "not recorded"])
    if used & {"run_daq", "compare_daq_stages"}:
        rows.append(["DAQ", "included with CryoPilot"])
    if "run_qscore" in used:
        rows.append(["Q-score", "included with CryoPilot"])
    if used & {"render_pdb", "render_map_views", "render_map_model"}:
        for name, label in (("pymol", "PyMOL"), ("chimerax", "ChimeraX")):
            if path_of(name):
                rows.append([label, path_of(name)])
    if used & {"submit_cryozeta", "download_cryozeta_result", "check_cryozeta_job", "wait_for_cryozeta"}:
        rows.append(["CryoZeta", "web server"])
    return rows


def modeling_methods(runs: list[dict[str, Any]], validation: dict[str, Any] | None, daq: dict[str, Any] | None, agent: dict[str, Any]) -> str:
    """A draft Methods paragraph describing what the modeling agent did."""

    counts: dict[str, int] = {}
    for run in runs:
        if run["output"] is not None or run["text"]:
            counts[run["name"]] = counts.get(run["name"], 0) + 1

    def rounds(name: str) -> str:
        return "one round" if counts.get(name) == 1 else f"{counts.get(name)} rounds"

    model = agent.get("model")
    used = f" using {model}" + (f" at {agent['effort']} reasoning effort" if agent.get("effort") else "") if model else ""
    sentences = [f"The atomic model was built by the modeling agent of CryoPilot {VERSION}{used}."]
    if counts.get("download_cryozeta_result") or counts.get("submit_cryozeta"):
        sentences.append("Candidate structures were predicted with CryoZeta.")
    if counts.get("download_pdb_homolog"):
        sentences.append("Homologous structures were taken from the PDB.")
    if counts.get("compare_map_hands") or counts.get("flip_map"):
        sentences.append("The map hand was checked.")
    if counts.get("align_model_to_map"):
        sentences.append("Models were fitted into the map with VESPER.")
    steps = []
    if counts.get("refine_phenix"):
        steps.append(f"{rounds('refine_phenix')} of real-space refinement in Phenix")
    if counts.get("refine_rosetta"):
        steps.append(f"{rounds('refine_rosetta')} of relaxation in Rosetta")
    if counts.get("edit_model"):
        steps.append(f"{rounds('edit_model')} of model editing")
    if steps:
        sentences.append(f"The model went through {_list(steps)}.")
    if validation:
        facts = []
        if validation.get("CC_mask") is not None:
            facts.append(f"a CC_mask of {validation['CC_mask']:.3f}")
        if validation.get("molprobity_score") is not None:
            facts.append(f"a MolProbity score of {validation['molprobity_score']:.2f}")
        if validation.get("clashscore") is not None:
            facts.append(f"a clashscore of {validation['clashscore']:.2f}")
        if validation.get("ramachandran_favored_pct") is not None:
            facts.append(f"{validation['ramachandran_favored_pct']:.2f}% Ramachandran favored")
        if validation.get("ramachandran_outliers_pct") is not None:
            facts.append(f"{validation['ramachandran_outliers_pct']:.2f}% Ramachandran outliers")
        if validation.get("rotamer_outliers_pct") is not None:
            facts.append(f"{validation['rotamer_outliers_pct']:.2f}% rotamer outliers")
        if facts:
            resolution = f" at {validation['resolution']:g} Å" if validation.get("resolution") else ""
            sentences.append(f"Validation in Phenix{resolution} gave {_list(facts)}.")
    aa = _daq_summary(daq).get("aa")
    if aa and aa.get("mean") is not None:
        sentences.append(f"The mean DAQ(AA) score was {aa['mean']:.2f} over {aa.get('finite') or aa.get('count')} residues.")
    return " ".join(sentences)


def _daq_summary(daq: dict[str, Any] | None) -> dict[str, Any]:
    candidates = (daq or {}).get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        summary = candidates[0].get("summary")
        return summary if isinstance(summary, dict) else {}
    return {}


def _modeling_report(session: dict[str, Any]) -> dict[str, Any]:
    config = session.get("config") or {}
    project = store.get_project(session["project_id"])
    workspace_relative = config.get("workspace") or file_roots.modeling_workspace_relative(project["slug"])
    workspace = MODELING_SANDBOX_ROOT / workspace_relative
    events = store.list_modeling_events(session["id"])
    answers = [event for event in events if event["kind"] == "answer"]
    last_message = str(answers[-1]["payload"].get("text") or "") if answers else ""
    runs = _tool_runs(session["id"])
    usage_lines, usage = _usage_section(session)

    final, reason = choose_final_model(runs, last_message, workspace)
    digest = _digest(final)
    validation, validation_is_final = _measurement_for(runs, "validate_model_phenix", digest, workspace)
    daq, daq_is_final = _measurement_for(runs, "run_daq", digest, workspace)
    qscore, _qscore_is_final = _measurement_for(runs, "run_qscore", digest, workspace)
    support, _support_is_final = _measurement_for(runs, "residue_support", digest, workspace)

    lines = _header(session, "Modeling")
    lines += ["", "## Final model", ""]
    if final is None:
        lines.append("No model file from this session was found.")
    else:
        lines.append(f"**{final.name}.** Chosen because {reason}. Check that this is the model you want.")
        lines += ["", f"Path: {final}"]
    map_path = next(
        (run["arguments"].get("map_path") for run in reversed(runs) if run["name"] in {"validate_model_phenix", "refine_phenix", "run_daq"} and run["arguments"].get("map_path")),
        None,
    )
    if map_path:
        lines += ["", f"Map: {MODELING_SANDBOX_ROOT / str(map_path) if not str(map_path).startswith('/') else map_path}"]

    lines += ["", "## Validation", ""]
    rows = []
    if validation:
        for key, label in (
            ("resolution", "Resolution used (Å)"),
            ("CC_mask", "CC_mask"),
            ("CC_box", "CC_box"),
            ("CC_volume", "CC_volume"),
            ("CC_peaks", "CC_peaks"),
            ("molprobity_score", "MolProbity score"),
            ("clashscore", "Clashscore"),
            ("ramachandran_favored_pct", "Ramachandran favored (%)"),
            ("ramachandran_outliers_pct", "Ramachandran outliers (%)"),
            ("rotamer_outliers_pct", "Rotamer outliers (%)"),
            ("cbeta_deviations", "C-beta deviations"),
        ):
            if validation.get(key) is not None:
                rows.append([label, validation[key]])
    for channel, label in (("aa", "Mean DAQ(AA)"), ("ca", "Mean DAQ(CA)"), ("ss", "Mean DAQ(SS)")):
        mean = (_daq_summary(daq).get(channel) or {}).get("mean")
        if mean is not None:
            rows.append([label, f"{mean:.3f}"])
    if qscore:
        for key in ("mean_qscore", "qscore", "mean"):
            if isinstance(qscore.get(key), (int, float)):
                rows.append(["Mean Q-score", f"{qscore[key]:.3f}"])
                break
    if support:
        if support.get("residues") is not None:
            rows.append(["Residues", support["residues"]])
        if support.get("residues_mostly_outside_density") is not None:
            rows.append(["Residues mostly outside the density", support["residues_mostly_outside_density"]])
    if rows:
        lines.append(_table(["Measure", "Value"], rows))
        checked = (validation and not validation_is_final) or (daq and not daq_is_final)
        if checked and final is not None:
            lines += ["", f"Some of these numbers are from the last model the tools measured, which is not the same file as {final.name}."]
    else:
        lines.append("This session has no validation results.")

    methods = modeling_methods(runs, validation, daq, {"model": config.get("model"), "effort": config.get("reasoning_effort")}) if runs else ""
    if methods:
        lines += ["", "## Draft Methods", "", methods]

    if runs:
        lines += ["", "## Tools the agent ran", ""]
        tally: dict[str, int] = {}
        for run in runs:
            tally[run["name"]] = tally.get(run["name"], 0) + 1
        lines.append(_table(["Tool", "Times"], [[name, count] for name, count in tally.items()]))
        refinements = [run for run in runs if run["name"] in {"refine_phenix", "refine_rosetta"}]
        if refinements:
            rows = []
            for run in refinements:
                settings = {key: value for key, value in run["arguments"].items() if key not in {"model_path", "map_path", "output_dir", "label"}}
                rows.append([run["name"], Path(str(run["arguments"].get("model_path") or "")).name, ", ".join(f"{key}={value}" for key, value in settings.items())])
            lines += ["", "Refinement parameters:", "", _table(["Tool", "Input model", "Parameters"], rows)]

    software_rows = [
        ["CryoPilot", VERSION],
        ["Model", f"{config.get('model') or 'not recorded'}" + (f", {config.get('reasoning_effort')} reasoning effort" if config.get("reasoning_effort") else "")],
        *_software_versions(runs),
    ]
    lines += ["", "## Software", "", _table(["Item", "Version"], software_rows)]
    lines += ["", f"Started {_when(session['created_at'])}; last activity {_when(session['updated_at'])}."]
    lines += ["", *usage_lines]
    return _result(
        session,
        lines,
        methods,
        final={"path": str(final), "reason": reason} if final else None,
        fsc_curve=None,
        usage=usage,
        error=None,
    )


def build_report(session: dict[str, Any]) -> dict[str, Any]:
    return _reconstruction_report(session) if session["kind"] == "reconstruction" else _modeling_report(session)


# ---------------------------------------------------------------- transcript
def _fenced(text: str, language: str = "") -> str:
    """A code block whose fence is longer than any run of backticks in the text."""

    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _text_of(content: Any) -> tuple[str, int]:
    """The text of a message's content, and how many images were left out."""

    if isinstance(content, str):
        return content, 0
    images = 0
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "input_image":
                    images += 1
                elif part.get("text"):
                    parts.append(str(part["text"]))
    return "\n".join(parts), images


def _image_note(count: int) -> str:
    return f"_[{count} image{'' if count == 1 else 's'} left out]_"


def _harness_item_lines(origin: str, payload: Any, created_at: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    item_type = payload.get("type")
    role = payload.get("role")
    when = _when(created_at)
    if item_type == "function_call":
        arguments = payload.get("arguments")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            parsed = arguments
        lines = [f"### Tool: {payload.get('name') or 'tool'} ({when})", ""]
        if isinstance(parsed, dict) and isinstance(parsed.get("code"), str):
            lines.append(_fenced(parsed["code"], "python"))
            parsed = {key: value for key, value in parsed.items() if key != "code"}
        if parsed:
            lines += ["", _fenced(json.dumps(parsed, indent=2, ensure_ascii=False) if not isinstance(parsed, str) else parsed)]
        return lines
    if item_type == "function_call_output":
        output = payload.get("output")
        try:
            parsed = json.loads(output) if isinstance(output, str) else output
        except ValueError:
            parsed = output
        lines = [f"**Result** ({when})", ""]
        if isinstance(parsed, dict):
            shown = False
            for key in ("stdout", "stderr", "traceback", "error"):
                value = parsed.get(key)
                if value:
                    lines += [f"{key}:", "", _fenced(str(value)), ""]
                    shown = True
            others = {key: value for key, value in parsed.items() if key not in {"stdout", "stderr", "traceback", "error", "ok"} and value not in (None, "", [], {})}
            if others:
                lines += [_fenced(json.dumps(others, indent=2, ensure_ascii=False, default=str)), ""]
                shown = True
            if not shown:
                lines.append("(no output)")
        else:
            lines.append(_fenced(str(parsed)))
        return lines
    if item_type == "reasoning":
        texts = [str(entry.get("text") or "") for entry in payload.get("summary") or [] if isinstance(entry, dict)]
        text = "\n\n".join(part for part in texts if part)
        return [f"### Reasoning ({when})", "", text] if text else []
    text, images = _text_of(payload.get("content"))
    if role == "assistant":
        return [f"### Agent ({when})", "", text]
    if role == "user":
        heading = "Image for the agent" if images else "You" if origin != "bootstrap" else "Task"
        lines = [f"### {heading} ({when})", "", text] if text else [f"### {heading} ({when})", ""]
        if images:
            lines += ["", _image_note(images)]
        return lines
    if text.startswith(COMPACTION_PREFIX):
        return [f"### Context compacted ({when})", "", text]
    if text.startswith("[SESSION RESUME]"):
        return [f"### Session resumed ({when})", "", text]
    return [f"### Instructions to the agent ({when})", "", "_[left out; they are the same for every session]_"]


def _reconstruction_transcript(session: dict[str, Any]) -> Iterator[str]:
    harness_id = session.get("harness_session_id")
    if not harness_id:
        yield "This session has no conversation yet.\n"
        return
    try:
        for origin, payload, created_at in trace_reader.raw_items(harness_id):
            lines = _harness_item_lines(origin, payload, created_at)
            if lines:
                yield "\n".join(lines) + "\n\n"
        calls = trace_reader.recursive_calls(harness_id)
        for call in calls:
            yield f"## Sub-agent {call['id']}: {call['status']} ({_when(call['started_at'])})\n\n"
            for origin, payload, created_at in trace_reader.raw_recursive_items(harness_id, call["id"]):
                lines = _harness_item_lines(origin, payload, created_at)
                if lines:
                    yield "\n".join(lines) + "\n\n"
    except TraceError as exc:
        yield f"\nThe rest of the transcript could not be read: {exc}\n"


def _modeling_transcript(session: dict[str, Any]) -> Iterator[str]:
    for event in store.list_modeling_events(session["id"]):
        payload = event["payload"] if isinstance(event["payload"], dict) else {}
        when = _when(event["created_at"])
        kind = event["kind"]
        if kind == "user":
            yield f"### You ({when})\n\n{payload.get('text') or ''}\n\n"
        elif kind == "answer":
            yield f"### Agent ({when})\n\n{payload.get('text') or ''}\n\n"
        elif kind == "reasoning":
            yield f"### Reasoning ({when})\n\n{payload.get('text') or ''}\n\n"
        elif kind == "tool_call":
            arguments = json.dumps(payload.get("arguments"), indent=2, ensure_ascii=False, default=str)
            yield f"### Tool: {payload.get('name') or 'tool'} ({when})\n\n{_fenced(arguments, 'json')}\n\n"
        elif kind == "tool_output":
            parts = [f"**Result** ({when})", "", _fenced(str(payload.get("text") or "(no output)"))]
            if payload.get("images"):
                parts += ["", _image_note(len(payload["images"]))]
            yield "\n".join(parts) + "\n\n"
        elif kind == "error":
            yield f"### Error ({when})\n\n{payload.get('message') or payload.get('text') or ''}\n\n"


def transcript_markdown(session: dict[str, Any]) -> Iterable[str]:
    project = store.get_project(session["project_id"])
    yield f"# Transcript: {session['title']}\n\nProject {project['title']}. Exported by CryoPilot {VERSION} on {_when(datetime.now(timezone.utc).isoformat())}.\n\n"
    yield from (_reconstruction_transcript(session) if session["kind"] == "reconstruction" else _modeling_transcript(session))


def _without_images(value: Any) -> Any:
    """A copy with embedded images replaced by a note, because they can be hundreds of megabytes."""

    if isinstance(value, dict):
        if value.get("type") == "input_image":
            return {"type": "input_image", "image_url": "[left out]"}
        return {key: _without_images(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_images(item) for item in value]
    if isinstance(value, str) and value.startswith("data:image/"):
        return "[image left out]"
    return value


def transcript_jsonl(session: dict[str, Any]) -> Iterable[str]:
    """The records as stored, one JSON object per line, without embedded images."""

    def line(record: dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False, default=str) + "\n"

    if session["kind"] == "modeling":
        for event in store.list_modeling_events(session["id"]):
            yield line({"kind": event["kind"], "created_at": event["created_at"], "payload": _without_images(event["payload"])})
        return
    harness_id = session.get("harness_session_id")
    if not harness_id:
        return
    try:
        for origin, payload, created_at in trace_reader.raw_items(harness_id):
            yield line({"origin": origin, "created_at": created_at, "payload": _without_images(payload)})
        for call in trace_reader.recursive_calls(harness_id):
            for origin, payload, created_at in trace_reader.raw_recursive_items(harness_id, call["id"]):
                yield line({"sub_agent": call["id"], "origin": origin, "created_at": created_at, "payload": _without_images(payload)})
    except TraceError as exc:
        yield line({"error": str(exc)})


def transcript_filename(session: dict[str, Any], extension: str) -> str:
    return f"{_slug(session['title'])}-transcript.{extension}"
