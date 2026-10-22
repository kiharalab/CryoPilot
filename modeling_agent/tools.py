from __future__ import annotations

import asyncio
import atexit
import base64
import json
import mimetypes
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from agents import ToolOutputImage, ToolOutputText, function_tool
from PIL import Image

from cryozeta_client import CryoZetaClient, CryoZetaError
from map_utils import flip_map_copy, read_mrc
import map_views
import refinement
from pdb_homologs import search_homologs, download_homolog
from handedness import compare_scores, compare_stage_scores, model_provenance
import vesper_align

CODE_ROOT = Path(__file__).resolve().parent
# File tools are confined to ROOT. It is the agent directory itself unless
# MODELING_AGENT_SANDBOX points elsewhere (a packaged build ships read-only code).
ROOT = Path(os.environ.get("MODELING_AGENT_SANDBOX") or CODE_ROOT).expanduser().resolve()
DAQ_SCRIPT = CODE_ROOT / "daq" / "daq_compute.py"
SUPPORT_SCRIPT = CODE_ROOT / "chimerax_support.py"
MAX_TEXT_OUTPUT = 24_000
CRYOZETA_POLL_INTERVAL_SECONDS = 30 * 60
CRYOZETA_SUCCESS_STATUSES = frozenset(
    {"complete", "completed", "finished", "succeeded", "success"}
)
CRYOZETA_FAILURE_STATUSES = frozenset(
    {"cancelled", "canceled", "error", "failed", "failure", "terminated"}
)
SENSITIVE_ENVIRONMENT_MARKERS = (
    "API_KEY",
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)


def _workspace_path(path: str, *, must_exist: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(
            f"Path must stay inside the modeling-agent workspace: {ROOT}"
        ) from exc
    if must_exist and not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved


def _trim(text: str, limit: int = MAX_TEXT_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated {len(text) - limit} characters"


def _shell_environment() -> dict[str, str]:
    """Return the process environment without credential-like values."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in SENSITIVE_ENVIRONMENT_MARKERS)
    }
    environment.pop("BASH_ENV", None)
    environment.pop("ENV", None)
    return environment


def _run_bash(
    command: str,
    *,
    working_directory: str = ".",
    timeout_seconds: int = 300,
) -> str:
    if not command.strip():
        raise ValueError("Bash command must not be empty.")
    cwd = _workspace_path(working_directory, must_exist=True)
    if not cwd.is_dir():
        raise ValueError(f"Not a directory: {cwd}")
    timeout = max(1, min(int(timeout_seconds), 1_800))
    # Detach from the controlling terminal: an interactive launcher run by a
    # command (for example '#!/bin/bash -i' PyMOL on Zebra) must not be able to
    # stop the agent's process group when several tool calls run concurrently.
    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_bash_tool_environment(),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill background children too, so none keeps the output pipes open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        return json.dumps(
            {
                "exit_code": None,
                "timed_out": True,
                "timeout_seconds": timeout,
                "working_directory": str(cwd),
                "stdout": _trim(stdout, MAX_TEXT_OUTPUT // 2),
                "stderr": _trim(stderr, MAX_TEXT_OUTPUT // 2),
            },
            indent=2,
        )
    return json.dumps(
        {
            "exit_code": process.returncode,
            "timed_out": False,
            "working_directory": str(cwd),
            "stdout": _trim(stdout, MAX_TEXT_OUTPUT // 2),
            "stderr": _trim(stderr, MAX_TEXT_OUTPUT // 2),
        },
        indent=2,
    )


def _image_output(path: Path) -> ToolOutputImage:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return ToolOutputImage(
        image_url=f"data:{mime};base64,{payload}",
        detail="high",
    )


@function_tool
def read_file(path: str, max_characters: int = 24_000) -> str:
    """Read a UTF-8 text file from the workspace.

    Args:
        path: Workspace-relative or absolute in-workspace file path.
        max_characters: Maximum characters returned.
    """
    target = _workspace_path(path, must_exist=True)
    if not target.is_file():
        raise ValueError(f"Not a file: {target}")
    limit = max(1, min(int(max_characters), 100_000))
    return _trim(target.read_text(encoding="utf-8", errors="replace"), limit)


@function_tool
def write_file(path: str, content: str) -> str:
    """Write a UTF-8 text file inside the workspace.

    Args:
        path: Workspace-relative or absolute in-workspace output path.
        content: Complete file content.
    """
    target = _workspace_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {target}"


@function_tool
def bash(
    command: str,
    working_directory: str = ".",
    timeout_seconds: int = 300,
) -> str:
    """Run a Bash command for local structure-modeling work.

    Commands start in the modeling-agent workspace and receive a credential-
    scrubbed environment. Output is bounded and includes the exit code. Keep all
    reads and writes inside the workspace; do not run destructive system commands.

    Args:
        command: Bash command or short script to execute.
        working_directory: Workspace-relative or absolute in-workspace directory.
        timeout_seconds: Runtime limit from 1 to 1800 seconds.
    """
    return _run_bash(
        command,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
    )


@function_tool
def grep(pattern: str, path: str = ".", glob: str = "*", max_results: int = 100) -> str:
    """Search workspace text files with ripgrep.

    Args:
        pattern: Regular expression to search for.
        path: Workspace-relative file or directory.
        glob: File glob such as '*.csv' or '*.py'.
        max_results: Maximum matching lines returned.
    """
    target = _workspace_path(path, must_exist=True)
    limit = max(1, min(int(max_results), 500))
    command = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--glob",
        glob,
        "--max-count",
        str(limit),
        pattern,
        str(target),
    ]
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        start_new_session=True, check=False
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError(completed.stderr.strip() or "ripgrep failed")
    lines = completed.stdout.splitlines()[:limit]
    return "\n".join(lines) if lines else "No matches."


@function_tool
def view_image(path: str) -> list[ToolOutputText | ToolOutputImage]:
    """Load a PNG, JPEG, WEBP, or GIF into model context.

    Args:
        path: Workspace-relative or absolute in-workspace image path.
    """
    target = _workspace_path(path, must_exist=True)
    with Image.open(target) as image:
        width, height = image.size
        image.verify()
    return [
        ToolOutputText(text=f"Loaded image {target} ({width}x{height})."),
        _image_output(target),
    ]


@function_tool
def run_daq(
    model_path: str,
    map_path: str,
    output_dir: str = "outputs/daq",
    contour: float = 0.0,
    batch_size: int = 0,
    backend: str = "auto",
    half_window: int = 0,
    candidate_index: int = 0,
) -> list[ToolOutputText | ToolOutputImage]:
    """Run DAQ with automatic 1 Å resampling and separate candidate/chain profiles.

    DAQ scores every 1 Å grid point at or above half the contour (no stride and
    no random subsampling), so repeated runs with the same inputs give the same scores.

    CSV AA/CA/SS/total and summaries are raw residue scores; smoothing is
    separately labeled. Missing scores remain missing and have zero occupancy
    in annotated structures. SS is a C-alpha heuristic, not DSSP; total is only
    an arithmetic average. Inspect model-density overlays as well as scores.

    Args:
        model_path: Input PDB or mmCIF model inside the workspace.
        map_path: Input MRC/MAP density map inside the workspace.
        output_dir: Workspace output directory for the DAQ artifacts.
        contour: Map level that DAQ treats as density when sampling points; use the level you judge to enclose the molecule, and keep it fixed when comparing versions of a model.
        batch_size: ONNX batch size; 0 selects automatically.
        backend: DAQ backend: auto, cpu, cuda, tensorrt, directml, mlx, or mlx-cpu.
        half_window: Optional smoothing half-window written to the *_smoothed columns; raw scores are always kept.
        candidate_index: 1-based candidate to score; 0 scores all separately.
    """
    model = _workspace_path(model_path, must_exist=True)
    density_map = _workspace_path(map_path, must_exist=True)
    destination = _workspace_path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    if candidate_index < 0 or half_window < 0 or not math.isfinite(contour):
        raise ValueError("Invalid candidate index, smoothing window, or contour.")

    command = [
        sys.executable,
        str(DAQ_SCRIPT),
        "--model",
        str(model),
        "--map",
        str(density_map),
        "--output-dir",
        str(destination),
        "--contour",
        str(float(contour)),
        "--stride",
        "1",
        "--max-points",
        "0",
        "--batch-size",
        str(max(0, int(batch_size))),
        "--backend",
        backend,
        "--half-window",
        str(max(0, int(half_window))),
        "--candidate-index",
        str(int(candidate_index)),
        "--model-path",
        str(CODE_ROOT / "models" / "Multimodel.onnx"),
    ]
    print(f"◆ DAQ: validating {model.name} against {density_map.name}", flush=True)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=1_800,
        stdin=subprocess.DEVNULL,
        env=os.environ.copy(),
        start_new_session=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            _trim(
                f"DAQ failed with exit code {completed.returncode}\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
        )

    json_line = next(
        (
            line
            for line in reversed(completed.stdout.splitlines())
            if line.lstrip().startswith("{")
        ),
        None,
    )
    if json_line is None:
        raise RuntimeError(
            f"DAQ did not return a JSON result.\n{_trim(completed.stdout)}"
        )
    result = json.loads(json_line)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "DAQ failed"))

    outputs = [ToolOutputText(text=json.dumps(result, indent=2))]
    # Avoid flooding context for a large ensemble; every artifact path remains
    # available and the agent can inspect additional candidates with view_image.
    for candidate in result["candidates"][:5]:
        outputs.append(_image_output(_workspace_path(candidate["scorePlot"], must_exist=True)))
    return outputs


def _pymol_quote(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


@function_tool
def render_pdb(
    pdb_path: str,
    output_path: str = "outputs/pdb_render.png",
    color_by_bfactor: bool = False,
    width: int = 1200,
    height: int = 900,
) -> list[ToolOutputText | ToolOutputImage]:
    """Render a PDB to a non-ray-traced PNG with headless PyMOL and show it to the model.

    Args:
        pdb_path: Input PDB inside the workspace.
        output_path: Workspace-relative PNG output path.
        color_by_bfactor: DAQ coloring: red low, white zero, blue high; unscored occupancy-zero atoms are gray.
        width: PNG width, from 320 to 2400.
        height: PNG height, from 320 to 2400.
    """
    model = _workspace_path(pdb_path, must_exist=True)
    output = _workspace_path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    pymol = _pymol_executable()

    width = max(320, min(int(width), 2400))
    height = max(320, min(int(height), 2400))
    color_command = (
        "spectrum b, red_white_blue, structure and polymer.protein, minimum=-1.5, maximum=1.5\ncolor gray70, structure and q=0"
        if color_by_bfactor
        else "util.cbc(structure)"
    )
    script = f"""reinitialize
load "{_pymol_quote(model)}", structure
hide everything
show cartoon, structure and polymer.protein
show sticks, structure and organic
set cartoon_fancy_helices, 1
set cartoon_smooth_loops, 1
set antialias, 2
set opaque_background, off
bg_color white
{color_command}
orient structure
viewport {width}, {height}
png {_pymol_quote(output)}, width={width}, height={height}, dpi=150, ray=0
quit
"""
    print(f"◆ PyMOL: rendering {model.name}", flush=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".pml",
        prefix="render_",
        dir=output.parent,
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        completed = subprocess.run(
            [pymol, "-cq", *_pymol_thread_arguments(), str(script_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            env=_shell_environment(),
            start_new_session=True,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if completed.returncode != 0 or not output.exists():
        raise RuntimeError(
            _trim(
                f"PyMOL failed with exit code {completed.returncode}\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
        )
    with Image.open(output) as rendered:
        if rendered.mode in {"RGBA", "LA"} or "transparency" in rendered.info:
            rgba = rendered.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            white.convert("RGB").save(output, format="PNG")
    return [
        ToolOutputText(text=f"Rendered non-ray-traced PNG to {output}."),
        _image_output(output),
    ]


@function_tool
def flip_map(map_path: str, output_path: str = "outputs/map_flipped.mrc", axis: str = "z") -> str:
    """Save a mirrored map copy for handedness comparison; never change the model.

    Independently realign the immutable-chirality model to both hands, visually
    inspect density overlays, and compare DAQ with equivalent settings/coverage.
    A score increase supports but does not prove a handedness correction.

    Args:
        map_path: Original workspace MRC/MAP file.
        output_path: Different workspace path for the mirrored map.
        axis: Physical reflection axis: x, y, or z.
    """
    source = _workspace_path(map_path, must_exist=True)
    target = _workspace_path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = flip_map_copy(source, target, axis)
    target.with_suffix(".reflection.json").write_text(json.dumps(result, indent=2))
    return json.dumps(result, indent=2)


def _interactive_bash_launcher(path: Path) -> bool:
    """Interactive Bash wrappers can stop the agent's process group in tmux."""

    try:
        with path.open("rb") as handle:
            shebang = handle.readline(256)
    except OSError:
        return False
    if not shebang.startswith(b"#!") or b"bash" not in shebang:
        return False
    return any(
        flag.startswith(b"-") and not flag.startswith(b"--") and b"i" in flag[1:]
        for flag in shebang.split()[1:]
    )


def _pymol_thread_arguments() -> list[str]:
    """Cap PyMOL ray-tracing threads; by default it starts one per CPU core.

    Several concurrent agents each rendering with every core oversubscribe the
    node. CRYOAGENT_PYMOL_THREADS overrides the default of 4.
    """

    try:
        threads = int(os.environ.get("CRYOAGENT_PYMOL_THREADS", "4"))
    except ValueError:
        threads = 4
    return ["-d", f"set max_threads, {max(1, threads)}"]


def _validated_pymol_executable(candidate: str) -> str:
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"PyMOL executable is missing or not executable: {path}")
    if _interactive_bash_launcher(path):
        raise RuntimeError(
            f"PyMOL launcher {path} starts interactive Bash; use the module-provided "
            "executable or set PYMOL_EXECUTABLE to its path."
        )
    return str(path)


def _pymol_executable() -> str:
    configured = os.environ.get("PYMOL_EXECUTABLE") or os.environ.get("PYMOL_EXE")
    if configured:
        return _validated_pymol_executable(configured)

    candidate = shutil.which("pymol")
    if candidate and not _interactive_bash_launcher(Path(candidate).resolve()):
        return _validated_pymol_executable(candidate)

    # Zebra's default /usr/local/bin/pymol invokes '#!/bin/bash -i'. Resolve
    # the module's noninteractive executable without running that wrapper.
    try:
        result = subprocess.run(
            [
                "/bin/bash", "-lc",
                'module load pymol >/dev/null 2>&1 && printf "%s\\n" "${PYMOL_EXE:-$(command -v pymol)}"',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=_shell_environment(),
            start_new_session=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if paths:
            return _validated_pymol_executable(paths[-1])
    raise RuntimeError(
        "A noninteractive PyMOL executable is required. Load its module or set "
        "PYMOL_EXECUTABLE to the module-provided path."
    )


_LAUNCHER_DIRECTORY: Path | None = None
_LAUNCHER_LOCK = threading.Lock()


def _noninteractive_launcher_directory() -> Path:
    """Return a PATH directory whose pymol/chimerax skip interactive wrappers.

    Agent-written bash commands call bare 'pymol' or 'chimerax'. Wrapper scripts
    here exec the resolved noninteractive executables; the module bin directories
    are not put on PATH because they also contain their own 'python'.
    """

    global _LAUNCHER_DIRECTORY
    with _LAUNCHER_LOCK:
        if _LAUNCHER_DIRECTORY is None:
            _LAUNCHER_DIRECTORY = _write_launchers()
        return _LAUNCHER_DIRECTORY


def _write_launchers() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="cryoagent_launchers_"))
    atexit.register(shutil.rmtree, directory, True)
    for names, resolve, arguments in ((("pymol",), _pymol_executable, _pymol_thread_arguments()),
                                      (("chimerax", "ChimeraX"), _chimerax_executable, [])):
        try:
            executable = resolve()
        except RuntimeError:
            continue
        command = shlex.join([executable, *arguments])
        for name in names:
            launcher = directory / name
            launcher.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
            launcher.chmod(0o755)
    return directory


def _program_directories() -> list[str]:
    """Phenix and Rosetta bin directories, so the agent can run any of their programs from bash."""
    found = []
    for env_name, fallback in (("PHENIX_BIN", refinement.ZEBRA_PHENIX_BIN), ("ROSETTA_BIN", refinement.ZEBRA_ROSETTA_BIN)):
        directory = os.environ.get(env_name) or fallback
        if directory and Path(directory).is_dir():
            found.append(directory)
    return found


def _bash_tool_environment() -> dict[str, str]:
    environment = _shell_environment()
    launchers = _noninteractive_launcher_directory()
    extra = ([str(launchers)] if any(launchers.iterdir()) else []) + _program_directories()
    if extra:
        environment["PATH"] = os.pathsep.join([*extra, environment.get("PATH", os.defpath)])
    if Path(refinement.ZEBRA_ROSETTA_DB).is_dir():
        environment.setdefault("ROSETTA3_DB", refinement.ZEBRA_ROSETTA_DB)
    return environment


def _validated_chimerax_executable(candidate: str) -> str:
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"ChimeraX executable is missing or not executable: {path}")
    if _interactive_bash_launcher(path):
        raise RuntimeError(
            f"ChimeraX launcher {path} starts interactive Bash; use the module-provided "
            "binary or set CHIMERAX_EXECUTABLE to its path."
        )
    return str(path)


def _chimerax_executable():
    configured = os.environ.get("CHIMERAX_EXECUTABLE")
    if configured:
        return _validated_chimerax_executable(configured)

    for name in ("chimerax", "ChimeraX"):
        candidate = shutil.which(name)
        if candidate:
            if _interactive_bash_launcher(Path(candidate).resolve()):
                continue
            return _validated_chimerax_executable(candidate)

    apps = sorted(Path("/Applications").glob("ChimeraX*.app/Contents/MacOS/ChimeraX"))
    if apps:
        return _validated_chimerax_executable(str(apps[-1]))

    # On Zebra the default /usr/local/bin/chimerax is a '#!/bin/bash -i'
    # wrapper. Resolve the module's actual binary in a noninteractive shell;
    # never run that interactive wrapper from concurrent agent tool calls.
    try:
        result = subprocess.run(
            ["/bin/bash", "-lc", "module load chimerax >/dev/null 2>&1 && command -v chimerax"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            env=_shell_environment(),
            start_new_session=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if candidates:
            return _validated_chimerax_executable(candidates[-1])
    raise RuntimeError(
        "ChimeraX is required for model-density simulation and Q-score calculation. "
        "Load its module or set CHIMERAX_EXECUTABLE to the noninteractive binary."
    )


def _run_chimerax_support(config):
    # The subprocess runs from its staging directory. Preserve the old ROOT-relative
    # path semantics before changing cwd.
    config = dict(config)
    for name in ("model_path", "map_path", "output_path"):
        if name in config:
            config[name] = str((ROOT / Path(config[name]).expanduser()).resolve())
    with tempfile.TemporaryDirectory(prefix="chimerax_", dir=Path(config["output_path"]).parent) as temporary:
        config = dict(config, result_path=str(Path(temporary) / "result.json"))
        config_path = Path(temporary) / "config.json"
        config_path.write_text(json.dumps(config))
        # Some cluster launchers forward arguments with unquoted $*, splitting
        # ChimeraX's combined "script.py config.json" argument. Use a local script
        # basename with no arguments; even workspace paths with spaces stay off argv.
        script_path = Path(temporary) / "chimerax_support.py"
        shutil.copyfile(SUPPORT_SCRIPT, script_path)
        completed = subprocess.run([_chimerax_executable(), "--nogui", "--exit", "--script", script_path.name],
                                   cwd=temporary, capture_output=True, text=True, timeout=1800,
                                   stdin=subprocess.DEVNULL, env=_shell_environment(),
                                   start_new_session=True, check=False)
        result_path = Path(config["result_path"])
        if completed.returncode or not result_path.exists():
            reason = f"exit code {completed.returncode}" if completed.returncode else "no result file"
            raise RuntimeError(f"ChimeraX support failed ({reason}): " + _trim(completed.stdout + completed.stderr))
        result = json.loads(result_path.read_text())
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "ChimeraX support failed."))
        return result


def _simulate_model_map(model, output, resolution):
    return _run_chimerax_support({"operation": "simulate", "model_path": str(model),
                                 "output_path": str(output), "resolution": resolution})


def _align_model(model_path, map_path, output_dir, resolution, contour, candidate_index=1,
                 grid_spacing=2.0, angular_step=30.0, top_n=10, bandwidth=4.0, cores=2):
    result = vesper_align.align(
        _workspace_path(model_path, must_exist=True), _workspace_path(map_path, must_exist=True),
        _workspace_path(output_dir), resolution=resolution, contour=contour,
        candidate_index=candidate_index, grid_spacing=grid_spacing, angular_step=angular_step,
        top_n=top_n, bandwidth=bandwidth, cores=cores, simulate=_simulate_model_map,
        environment=_shell_environment())
    return json.dumps(result, indent=2)


@function_tool
def align_model_to_map(model_path: str, map_path: str, resolution: float, contour: float,
                       output_dir: str = "outputs/alignment", candidate_index: int = 1,
                       grid_spacing: float = 2.0, angular_step: float = 30.0,
                       top_n: int = 10, bandwidth: float = 4.0, cores: int = 2) -> str:
    """Rigidly fit one candidate using VESPER and a simulated density map.

    Returns fitted mmCIF coordinates in map space, a proper rotation/translation,
    DOT scores and alternative placements. DOT is not a Q-score. When comparing the two
    map hands, fit with the same settings to both so the comparison is fair. ChimeraX
    generates the simulated map; VESPER performs the alignment. ChimeraX fitmap (from
    bash, `chimerax --nogui --offscreen --exit script.cxc`) is an alternative for local
    rigid-body fitting of a model that is already roughly placed.

    Args:
        model_path: Workspace PDB/mmCIF file, potentially multiple candidates.
        map_path: Workspace target MRC/MAP.
        resolution: Map resolution in Å for simulated model density.
        contour: Map level enclosing the molecule.
        output_dir: Workspace directory for fitted model and metadata.
        candidate_index: 1-based candidate to fit, never the ensemble as a whole.
        grid_spacing: VESPER translation sampling in Å, 0.5..10; reduce to 1 for closer inspection.
        angular_step: Global angular sampling in degrees, 5..90; VESPER refines top placements at 5 degrees.
        top_n: Number of VESPER placements to retain, 1..20.
        bandwidth: VESPER Gaussian bandwidth; use equivalent settings for both hands.
        cores: CPU threads, 1..32.
    """
    return _align_model(model_path, map_path, output_dir, resolution, contour, candidate_index,
                        grid_spacing, angular_step, top_n, bandwidth, cores)


@function_tool
def search_pdb_homologs(protein_fasta_path: str, output_dir: str = "outputs/pdb_search",
                        identity_cutoff: float = 0.3, max_hits: int = 5) -> str:
    """Search experimental PDB structures for each unique supplied protein sequence.

    Returns sequence identity, aligned query coverage, alignment evidence, copy counts and
    a saved report. Related structures are evidence (map hand, domain arrangement,
    plausibility); their coordinates must not be copied into the model. An API failure is
    not a no-homolog result.

    Args:
        protein_fasta_path: Complete assembly protein FASTA inside the workspace.
        output_dir: Directory for the PDB search evidence.
        identity_cutoff: Minimum sequence identity as a fraction from 0 to 1.
        max_hits: Maximum returned PDB polymer entities per unique sequence, 1..20.
    """
    return json.dumps(search_homologs(_workspace_path(protein_fasta_path, must_exist=True),
                                     _workspace_path(output_dir), identity_cutoff=identity_cutoff,
                                     max_hits=max_hits), indent=2)


@function_tool
def download_pdb_homolog(entity_id: str, output_dir: str = "outputs/pdb_reference") -> str:
    """Download one resolved protein copy of a PDB search hit as mmCIF.

    Args:
        entity_id: Experimental polymer entity identifier returned by search_pdb_homologs.
        output_dir: Separate directory for this reference and its provenance.
    """
    return json.dumps(download_homolog(entity_id, _workspace_path(output_dir)), indent=2)


@function_tool
def run_qscore(model_path: str, map_path: str, output_dir: str = "outputs/qscore") -> str:
    """Calculate ChimeraX Q-scores for one already VESPER-aligned PDB reference.

    Saves per-atom raw scores with missing coverage and reproducible settings.
    Use compare_map_hands with metric='qscore' to compare matched atoms. Q-score
    measures atom resolvability, not a calibrated handedness probability.

    Args:
        model_path: One fitted candidate or homolog, in map coordinates.
        map_path: Corresponding experimental map.
        output_dir: Separate output directory for each reference and map hand.
    """
    import gemmi
    model, density = _workspace_path(model_path, must_exist=True), _workspace_path(map_path, must_exist=True)
    if len(gemmi.read_structure(str(model))) != 1:
        raise ValueError("Score one candidate model at a time.")
    destination = _workspace_path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / "qscores.csv"
    if any(path.resolve() in (model, density) for path in (output, output.with_suffix(".metadata.json"), destination / "qscore_result.json")):
        raise ValueError("Q-score outputs must not overwrite inputs.")
    with tempfile.TemporaryDirectory(prefix="qscore_", dir=destination) as temporary:
        staged = Path(temporary) / "qscores.csv"
        result = _run_chimerax_support({"operation": "qscore", "model_path": str(model),
                                       "map_path": str(density), "output_path": str(staged)})
        shutil.copyfile(staged, output)
    result.update({"score_table": str(output), "map_path": str(density), "model_path": str(model),
                   "reference": model_provenance(model)})
    output.with_suffix(".metadata.json").write_text(json.dumps(dict(result, metric_type="qscore"), indent=2))
    (destination / "qscore_result.json").write_text(json.dumps(result, indent=2))
    return json.dumps(result, indent=2)


@function_tool
def compare_map_hands(original_scores_path: str, flipped_scores_path: str,
                      output_path: str = "outputs/handedness.json", metric: str = "aa", window: int = 19) -> str:
    """Compare the same model's scores in two map hands on exactly matched residues or atoms.

    For DAQ ('aa') it reports the mean of the centred `window`-residue AA DAQ over residues
    scored in both hands, plus coverage; for 'qscore' it compares atoms of a fitted reference.
    The tool reports the evidence; deciding the hand is up to you.

    Args:
        original_scores_path: Score CSV for the model fitted to one map hand.
        flipped_scores_path: Score CSV for the same model fitted to the other hand.
        output_path: JSON evidence report; a paired CSV is also saved.
        metric: 'aa' (DAQ) or 'qscore'.
        window: Residue window for the DAQ running mean (1 gives single-residue scores).
    """
    return json.dumps(compare_scores(_workspace_path(original_scores_path, must_exist=True),
                                     _workspace_path(flipped_scores_path, must_exist=True),
                                     _workspace_path(output_path), metric, window), indent=2)


@function_tool
def render_map_views(map_path: str, contour: float, lower: float = 0.0, higher: float = 0.0,
                     output_dir: str = "outputs/contour_views") -> list[ToolOutputText | ToolOutputImage]:
    """Draw contour-check views of a map at the selected level and one lower and one higher level.

    Each row shows three orthogonal projections of the map thresholded at that level
    and a central slice with the contour line, plus connected-component counts.
    Use it to confirm that all macromolecular density is enclosed and how much
    isolated noise remains. Rendering uses numpy and matplotlib, not OpenGL.

    Args:
        map_path: Workspace MRC/MAP file.
        contour: Selected contour level.
        lower: Lower comparison level; 0 uses 80% of the selected level.
        higher: Higher comparison level; 0 uses 120% of the selected level.
        output_dir: Directory for the PNG and statistics.
    """
    density = _workspace_path(map_path, must_exist=True)
    if not math.isfinite(contour):
        raise ValueError("Contour must be finite.")
    levels = [lower or contour * 0.8, contour, higher or contour * 1.2]
    if not all(math.isfinite(level) for level in levels):
        raise ValueError("Contour levels must be finite.")
    destination = _workspace_path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"contour_views_{contour:g}.png"
    if output.resolve() == density:
        raise ValueError("Render outputs must not overwrite inputs.")
    data, origin, step = read_mrc(density)
    stats = map_views.contour_views(data, origin, step, levels, output, title=density.name)
    metadata = {"map_path": str(density), "levels": levels, "selected": contour, "image": str(output),
                "component_stats": stats}
    (destination / f"contour_views_{contour:g}.json").write_text(json.dumps(metadata, indent=2))
    return [ToolOutputText(text=json.dumps(metadata, indent=2)), _image_output(output)]


@function_tool
def render_map_model(model_path: str, map_path: str, contour: float,
                     output_dir: str = "outputs/overlays", selection: str = "all",
                     color_by_bfactor: bool = False, slab_thickness: float = 12.0
                     ) -> list[ToolOutputText | ToolOutputImage]:
    """Draw three model-density overlays (orthogonal slabs) and return images for inspection.

    Each view is a density slab through the centre of the selection, shown as a
    maximum projection with the contour line, with the model atoms that lie in the
    slab overlaid (Cα circled). Use an already aligned single-candidate model; this
    tool does not align it. Inspect whole-model views and local backbone/helix regions
    by narrowing the selection. DAQ colors are red low / blue high, with unscored
    occupancy-zero atoms gray. Rendering uses numpy and matplotlib, not OpenGL.

    Args:
        model_path: Fitted workspace PDB/mmCIF, preferably DAQ-annotated.
        map_path: Corresponding workspace MRC/MAP in the same coordinates.
        contour: Selected density contour level.
        output_dir: Directory for three PNG views and metadata.
        selection: 'all', 'chain A0', 'resi 50-80', or terms joined with 'and'.
        color_by_bfactor: Color DAQ-annotated model by score, red low / blue high.
        slab_thickness: Slab thickness in angstroms (4 to 60).
    """
    model, density = _workspace_path(model_path, must_exist=True), _workspace_path(map_path, must_exist=True)
    if not math.isfinite(contour):
        raise ValueError("Contour must be finite.")
    if not 4 <= slab_thickness <= 60:
        raise ValueError("Slab thickness must be 4..60 Å.")
    destination = _workspace_path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    outputs = [destination / f"overlay_{view}.png" for view in ("front", "side", "top")]
    if any(p.resolve() in (model, density) for p in outputs):
        raise ValueError("Render outputs must not overwrite inputs.")
    atoms = map_views.read_atoms(model)
    data, origin, step = read_mrc(density)
    info = map_views.overlay_views(data, origin, step, atoms, contour, outputs, selection=selection,
                                   color_by_bfactor=color_by_bfactor, slab=slab_thickness)
    metadata = {"model_path": str(model), "map_path": str(density), "contour": contour,
                "selection": selection, "images": [str(p) for p in outputs], **info,
                "DAQ_scale": "red -1.5 / white 0 / blue +1.5; unscored gray" if color_by_bfactor else None}
    (destination / "overlay_metadata.json").write_text(json.dumps(metadata, indent=2))
    return [ToolOutputText(text=json.dumps(metadata, indent=2)), *[_image_output(p) for p in outputs]]


@function_tool
def residue_support(model_path: str, map_path: str, contour: float, output_csv: str,
                    daq_scores_path: str = "", candidate_index: int = 1, window: int = 19) -> str:
    """Per-residue evidence of density support, for deciding what to rebuild, refine or remove.

    For every residue: fraction of atoms above the contour, mean map value at its atoms
    relative to the contour, raw AA DAQ and its centred running mean (if a DAQ table is
    given), and Cα–Cα gaps that indicate chain breaks or implausible links. The summary
    lists runs of residues outside the density and runs with low DAQ. It changes nothing.
    DAQ leaves residues far from density unscored, so check the density columns for them.

    Args:
        model_path: Single-candidate model in the same frame as the map.
        map_path: Map the model was built into.
        contour: Level that you treat as the molecular envelope.
        output_csv: Path for the per-residue table (a .json summary is written beside it).
        daq_scores_path: Optional daq_scores.csv for this model.
        candidate_index: Candidate rows to use from the DAQ table.
        window: Residue window for the DAQ running mean.
    """
    if not math.isfinite(contour) or contour == 0:
        raise ValueError("Give a finite, nonzero contour.")
    scores = _workspace_path(daq_scores_path, must_exist=True) if daq_scores_path.strip() else None
    return json.dumps(refinement.residue_support(_workspace_path(model_path, must_exist=True),
                                                 _workspace_path(map_path, must_exist=True), contour,
                                                 _workspace_path(output_csv), scores, candidate_index, window), indent=2)


@function_tool
def edit_model(model_path: str, output_path: str, operations_json: str) -> str:
    """Edit a model explicitly and write a new file (the input is never changed).

    `operations_json` is a JSON list applied in order, for example:
      [{"op": "delete", "chain": "A0", "residues": "1-44"},
       {"op": "split", "chain": "A0", "after": 494, "new_chain": "A1"},
       {"op": "move_residues", "chain": "A1", "residues": "495-647", "to_chain": "B9"},
       {"op": "rename_chain", "chain": "B9", "to": "B1"}]
    'delete' without 'residues' removes the whole chain. For anything else (merging candidates,
    renumbering, changing occupancies) write a short gemmi script with bash, or use
    phenix.pdbtools. Record why each edit was made.

    Args:
        model_path: Single-candidate input model.
        output_path: New .cif or .pdb path.
        operations_json: JSON list of operations as above.
    """
    try:
        operations = json.loads(operations_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"operations_json is not valid JSON: {exc}") from exc
    if not isinstance(operations, list) or not all(isinstance(o, dict) for o in operations):
        raise ValueError("operations_json must be a JSON list of objects.")
    return json.dumps(refinement.edit_model(_workspace_path(model_path, must_exist=True),
                                            _workspace_path(output_path), operations), indent=2)


@function_tool
def compare_daq_stages(before_scores_path: str, after_scores_path: str,
                       output_path: str = "outputs/stage_comparison.json", window: int = 19) -> str:
    """Compare two versions of one model by AA DAQ on the residues scored in both.

    Both tables must come from run_daq against the same map with the same settings.
    Residue sets may differ (for example after editing); only residues scored in both are
    used. Reports the windowed-mean and raw means and the coverage.

    Args:
        before_scores_path: daq_scores.csv of the earlier version.
        after_scores_path: daq_scores.csv of the later version.
        output_path: JSON report path.
        window: Residue window for the running mean (1 gives single-residue scores).
    """
    return json.dumps(compare_stage_scores(_workspace_path(before_scores_path, must_exist=True),
                                           _workspace_path(after_scores_path, must_exist=True),
                                           _workspace_path(output_path), window), indent=2)


@function_tool
def validate_model_phenix(model_path: str, map_path: str, resolution: float,
                          output_dir: str = "outputs/phenix_validation") -> str:
    """Map-model correlation (phenix.map_correlations) and geometry (phenix.molprobity) for one model.

    Reports CC_mask, CC_box, CC_volume, CC_peaks, MolProbity score, clashscore,
    Ramachandran outliers and favored, rotamer outliers and C-beta deviations. Other
    validation programs (phenix.validation_cryoem, phenix.mtriage for model-map FSC,
    phenix.emringer, phenix.cablam) can be run from bash.

    Args:
        model_path: Single-candidate workspace model.
        map_path: Map in the same frame as the model.
        resolution: Map resolution in angstroms.
        output_dir: Directory for logs and validation.json.
    """
    model, density = _workspace_path(model_path, must_exist=True), _workspace_path(map_path, must_exist=True)
    if not 0.5 <= resolution <= 20:
        raise ValueError("Resolution must be 0.5..20 Å.")
    print(f"◆ Phenix: validating {model.name}", flush=True)
    return json.dumps(refinement.validate_phenix(model, density, resolution, _workspace_path(output_dir)), indent=2)


@function_tool
def refine_phenix(model_path: str, map_path: str, resolution: float, output_dir: str, run: str = "",
                  macro_cycles: int = 5, nproc: int = 4, extra_parameters: list[str] | None = None,
                  label: str = "rsr") -> str:
    """Real-space refinement with phenix.real_space_refine, configured by you.

    `run` is a '+'-joined choice of minimization_global, rigid_body, local_grid_search,
    morphing, simulated_annealing, adp, occupancy and nqh_flips (empty = Phenix defaults:
    minimization_global+local_grid_search+adp+occupancy+nqh_flips). `extra_parameters` are
    PHIL assignments passed through unchanged, e.g. "secondary_structure.enabled=True",
    "ramachandran_plot_restraints.enabled=False", "reference_coordinate_restraints.enabled=True",
    "ncs_constraints=False", "weight=50", "rotamers.fit=all", "simulated_annealing=every_macro_cycle",
    "refinement.morphing=every_macro_cycle". Run `phenix.real_space_refine --show-defaults=2`
    from bash to see every option. The command and log are saved in output_dir.

    Args:
        model_path: Single-candidate model to refine.
        map_path: Map in the same frame as the model.
        resolution: Map resolution in angstroms.
        output_dir: New directory for this refinement.
        run: Refinement steps joined by '+', or empty for Phenix defaults.
        macro_cycles: Number of macro cycles (1..50).
        nproc: Processors (1..16).
        extra_parameters: Further PHIL key=value assignments.
        label: Short tag used in output file names.
    """
    model, density = _workspace_path(model_path, must_exist=True), _workspace_path(map_path, must_exist=True)
    if not 0.5 <= resolution <= 20 or not 1 <= macro_cycles <= 50 or not 1 <= nproc <= 16:
        raise ValueError("Resolution 0.5..20 Å, macro_cycles 1..50 and nproc 1..16 are required.")
    print(f"◆ Phenix: real-space refining {model.name} ({run or 'defaults'})", flush=True)
    return json.dumps(refinement.refine_phenix(model, density, resolution, _workspace_path(output_dir), run,
                                               macro_cycles, nproc, extra_parameters or [], label), indent=2)


@function_tool
def refine_rosetta(model_path: str, map_path: str, resolution: float, output_dir: str,
                   repeats: int = 1, nstruct: int = 1, density_weight: float = 35.0, cartesian: bool = True) -> str:
    """Density-guided FastRelax in Rosetta (ref2015 or ref2015_cart plus elec_dens_fast).

    Lower density weights favour ideal geometry; higher weights pull harder into the map.
    Cartesian relax also idealizes bond lengths and angles; torsion-space relax is faster.
    Chains are renamed to single letters for Rosetta and restored in the output mmCIF;
    hydrogens are removed. Large assemblies take hours per structure. For other protocols
    (local rebuilding, constraints to the starting model) write a RosettaScripts XML and run
    rosetta_scripts from bash; the Rosetta database is in $ROSETTA3_DB.

    Args:
        model_path: Single-candidate model to relax.
        map_path: Map in the same frame as the model.
        resolution: Map resolution in angstroms.
        output_dir: New directory for this run.
        repeats: FastRelax repeats (1..8).
        nstruct: Number of relaxed models (1..10); all scores are returned and the lowest total score is written as the result.
        density_weight: elec_dens_fast weight (1..200).
        cartesian: Cartesian (True) or torsion-space (False) relax.
    """
    model, density = _workspace_path(model_path, must_exist=True), _workspace_path(map_path, must_exist=True)
    if not 0.5 <= resolution <= 20 or not 1 <= repeats <= 8 or not 1 <= nstruct <= 10 or not 1 <= density_weight <= 200:
        raise ValueError("Resolution 0.5..20 Å, repeats 1..8, nstruct 1..10 and density_weight 1..200 are required.")
    print(f"◆ Rosetta: density relax of {model.name}", flush=True)
    return json.dumps(refinement.refine_rosetta(model, density, resolution, _workspace_path(output_dir),
                                                repeats, nstruct, density_weight, cartesian), indent=2)


_CRYOZETA_SUBMISSIONS = 0


def _authenticated_cryozeta_client() -> CryoZetaClient:
    client = CryoZetaClient.from_environment()
    client.login_from_environment()
    return client


def _optional_workspace_file(path: str) -> Path | None:
    return _workspace_path(path, must_exist=True) if path.strip() else None


@function_tool
def submit_cryozeta(
    map_path: str,
    protein_fasta_path: str = "",
    dna_fasta_path: str = "",
    rna_fasta_path: str = "",
    contour_level: float = 0.0,
    resolution: float = 3.0,
    job_note: str = "",
) -> str:
    """Submit a CryoZeta structure-modeling job to the Kihara Lab EM server.

    CryoZeta builds the assembly into the density above `contour_level` and returns five
    candidate models. A job occupies the shared server for roughly one to several hours, so
    submit when you have chosen the map (hand, version) and contour you believe will give the
    best model, and resubmit only when you expect a better result. Each run may submit at most
    MODELING_MAX_CRYOZETA_JOBS jobs (default 3). Authentication is read from the environment;
    never pass credentials as arguments. Submission is an external side effect: call it only
    when the user has asked for modeling.

    Args:
        map_path: Workspace path to the .map or .mrc file to model into.
        protein_fasta_path: Protein FASTA of the complete assembly (one record per chain copy).
        dna_fasta_path: Optional DNA FASTA.
        rna_fasta_path: Optional RNA FASTA.
        contour_level: Map level enclosing the macromolecule, chosen by you.
        resolution: Map resolution in angstroms.
        job_note: Optional non-secret job description.
    """
    global _CRYOZETA_SUBMISSIONS
    limit = int(os.environ.get("MODELING_MAX_CRYOZETA_JOBS", "3"))
    if _CRYOZETA_SUBMISSIONS >= limit:
        raise RuntimeError(f"This run has already submitted {_CRYOZETA_SUBMISSIONS} CryoZeta jobs (limit {limit}); "
                           "continue with the results you have.")
    if not math.isfinite(contour_level) or contour_level <= 0:
        raise ValueError("Pass the positive contour level you chose for this map.")
    density_map = _workspace_path(map_path, must_exist=True)
    protein_fasta = _optional_workspace_file(protein_fasta_path)
    dna_fasta = _optional_workspace_file(dna_fasta_path)
    rna_fasta = _optional_workspace_file(rna_fasta_path)
    print(f"◆ CryoZeta: submitting {density_map.name}", flush=True)
    client = _authenticated_cryozeta_client()
    result = client.submit_job(
        map_path=density_map,
        contour_level=float(contour_level),
        resolution=float(resolution),
        protein_fasta=protein_fasta,
        dna_fasta=dna_fasta,
        rna_fasta=rna_fasta,
        job_note=job_note,
    )
    _CRYOZETA_SUBMISSIONS += 1
    result["submissions_used"] = f"{_CRYOZETA_SUBMISSIONS} of {limit}"
    return json.dumps(result, indent=2)


@function_tool
def check_cryozeta_job(job_id: str) -> str:
    """Check one CryoZeta job and return result metadata when it is ready.

    Args:
        job_id: Job identifier returned by submit_cryozeta.
    """
    print(f"◆ CryoZeta: checking job {job_id}", flush=True)
    client = _authenticated_cryozeta_client()
    status = client.job_status(job_id)
    if status["result_ready"]:
        status["result"] = client.result_metadata(job_id)
    return json.dumps(status, indent=2)


async def _wait_for_cryozeta_success(
    client: CryoZetaClient,
    job_id: str,
    *,
    poll_interval_seconds: float = CRYOZETA_POLL_INTERVAL_SECONDS,
    sleeper=asyncio.sleep,
) -> dict:
    interval = float(poll_interval_seconds)
    if interval <= 0:
        raise ValueError("CryoZeta poll interval must be greater than zero.")

    poll_count = 0
    while True:
        if poll_count:
            client.login_from_environment()
        status = client.job_status(job_id)
        poll_count += 1
        state = str(status.get("status") or "").strip().lower()

        if status.get("result_ready") or state in CRYOZETA_SUCCESS_STATUSES:
            status["poll_count"] = poll_count
            status["result"] = client.result_metadata(job_id)
            print(f"◆ CryoZeta: job {job_id} succeeded", flush=True)
            return status

        if state in CRYOZETA_FAILURE_STATUSES:
            message = status.get("message") or status.get("status") or "failed"
            raise CryoZetaError(f"CryoZeta job {job_id} failed: {message}")

        print(
            f"◆ CryoZeta: job {job_id} is {status.get('status', 'Unknown')}; "
            "checking again in 30 minutes",
            flush=True,
        )
        await sleeper(interval)


@function_tool
async def wait_for_cryozeta(job_id: str) -> str:
    """Block the agent loop until a CryoZeta job succeeds.

    The job is checked immediately and then every 30 minutes with no deadline.
    Authentication is refreshed between polls. A terminal server failure stops
    the wait with an error. Ctrl-C interrupts the local wait but does not cancel
    the remote CryoZeta job.

    Args:
        job_id: Job identifier returned by submit_cryozeta.
    """
    print(f"◆ CryoZeta: waiting for job {job_id}", flush=True)
    client = _authenticated_cryozeta_client()
    status = await _wait_for_cryozeta_success(client, job_id)
    return json.dumps(status, indent=2)


@function_tool
def download_cryozeta_result(job_id: str, output_path: str = "") -> str:
    """Download a completed CryoZeta mmCIF model into the workspace.

    Args:
        job_id: Completed CryoZeta job identifier.
        output_path: Optional workspace-relative .cif or .mmcif output path.
    """
    if output_path.strip():
        destination = _workspace_path(output_path)
    else:
        safe_job_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in job_id
        )
        destination = _workspace_path(
            f"outputs/cryozeta/{safe_job_id}_CryoZeta_model_all.cif"
        )
    if destination.suffix.lower() not in {".cif", ".mmcif"}:
        raise ValueError("CryoZeta result output must end in .cif or .mmcif.")
    print(f"◆ CryoZeta: downloading job {job_id}", flush=True)
    client = _authenticated_cryozeta_client()
    result = client.download_result(job_id, destination)
    return json.dumps(result, indent=2)


ALL_TOOLS = [
    read_file,
    write_file,
    bash,
    grep,
    view_image,
    search_pdb_homologs,
    download_pdb_homolog,
    run_qscore,
    compare_map_hands,
    run_daq,
    render_pdb,
    flip_map,
    align_model_to_map,
    render_map_model,
    render_map_views,
    residue_support,
    edit_model,
    compare_daq_stages,
    validate_model_phenix,
    refine_phenix,
    refine_rosetta,
    submit_cryozeta,
    check_cryozeta_job,
    wait_for_cryozeta,
    download_cryozeta_result,
]
