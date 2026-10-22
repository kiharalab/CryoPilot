"""VESPER map-to-map search, with rigid transforms applied to atomic models.

Transform convention is taken from VESPER_code/mrcfft.c::PrintTbl:
target_xyz = MTX @ source_xyz + T (physical angstrom coordinates).
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import gemmi
import numpy as np

from map_utils import read_mrc, resample_to_1a, write_mrc

ROOT = Path(__file__).resolve().parent
VESPER_REVISION = "0670f93e1164cec9dd74310af28b545fb2b3d84b"
FIT_LINE = re.compile(r"^#(\d+) R=\{[^}]+\} MTX=\{([^}]+)\} T=\{([^}]+)\} sco=\s*(\S+) zsco=\s*(\S+)", re.M)


def executable() -> str:
    configured = os.environ.get("VESPER_EXECUTABLE")
    candidate = configured or shutil.which("VESPER") or str(ROOT / ".deps" / "vesper" / "VESPER")
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("VESPER is required. Run python setup_vesper.py or set VESPER_EXECUTABLE to its binary.")
    return str(path)


def parse_fits(text: str) -> list[dict]:
    fits = {}
    for match in FIT_LINE.finditer(text):
        rank, matrix, shift, score, zscore = match.groups()
        rotation = np.array([float(v) for v in matrix.split()]).reshape(3, 3)
        translation = np.array([float(v) for v in shift.split()])
        if translation.shape != (3,) or not np.isfinite(rotation).all() or not np.isfinite(translation).all():
            raise ValueError("Invalid VESPER physical-coordinate transform.")
        if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5):
            raise ValueError("Refusing a VESPER transform that changes model chirality.")
        if not math.isfinite(float(score)):
            continue
        fits[int(rank)] = {"rank": int(rank), "rotation_translation_matrix": np.column_stack([rotation, translation]).tolist(),
                          "dot_score": float(score), "zscore": float(zscore) if math.isfinite(float(zscore)) else None}
    if not fits:
        raise RuntimeError("VESPER returned no valid fitted transforms; stale outputs will not be used.")
    return sorted(fits.values(), key=lambda fit: fit["dot_score"], reverse=True)


def transformed_structure(structure, matrix):
    fitted = structure.clone()
    matrix = np.asarray(matrix)
    transform = gemmi.Transform()
    transform.mat.fromlist(matrix[:, :3].tolist())
    transform.vec.fromlist(matrix[:, 3].tolist())
    fitted[0].transform_pos_and_adp(transform)
    return fitted


def align(model: Path, density: Path, destination: Path, *, resolution: float, contour: float,
          candidate_index: int, grid_spacing: float, angular_step: float, top_n: int,
          bandwidth: float, cores: int, simulate, environment: dict) -> dict:
    if not all(math.isfinite(v) and v > 0 for v in (resolution, grid_spacing, angular_step, bandwidth)):
        raise ValueError("Resolution, grid spacing, angular step and bandwidth must be finite and positive.")
    if not math.isfinite(contour) or not 0.5 <= grid_spacing <= 10 or not 5 <= angular_step <= 90:
        raise ValueError("Contour must be finite; grid spacing must be 0.5..10 Å and angular step 5..90 degrees.")
    if not 1 <= top_n <= 20 or not 1 <= cores <= 32:
        raise ValueError("top_n must be 1..20; cores must be 1..32.")
    structure = gemmi.read_structure(str(model))
    if not 1 <= candidate_index <= len(structure):
        raise ValueError(f"candidate_index must be 1..{len(structure)}.")
    selected = structure.clone()
    for index in reversed(range(len(selected))):
        if index != candidate_index - 1:
            del selected[index]
    binary = executable()
    destination.mkdir(parents=True, exist_ok=True)
    # Stage every run in a fresh directory so failed searches cannot return old fits.
    with tempfile.TemporaryDirectory(prefix="vesper_", dir=destination) as temporary:
        work = Path(temporary)
        selected.make_mmcif_document().write_file(str(work / "candidate_input.cif"))
        simulate(work / "candidate_input.cif", work / "model_density.mrc", resolution)
        data, origin, step = read_mrc(density)
        data, origin, step = resample_to_1a(data, origin, step)
        if not np.any(data > contour):
            raise ValueError("No experimental density exceeds the selected contour.")
        write_mrc(work / "map_unified.mrc", data, origin, step)
        command = [binary, "-a", "map_unified.mrc", "-b", "model_density.mrc",
                   "-t", str(contour), "-T", "0", "-s", str(grid_spacing),
                   "-A", str(angular_step), "-N", str(top_n), "-g", str(bandwidth), "-c", str(cores)]
        completed = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=1800,
                                   stdin=subprocess.DEVNULL, env=environment,
                                   start_new_session=True, check=False)
        if completed.returncode:
            raise RuntimeError("VESPER fitting failed: " + (completed.stdout + completed.stderr)[-4000:])
        fits = parse_fits(completed.stdout)
        (work / "vesper.log").write_text(completed.stdout + completed.stderr)
        for index, fit in enumerate(fits):
            filename = f"placement_{index:03d}.cif"
            transformed_structure(selected, fit["rotation_translation_matrix"]).make_mmcif_document().write_file(str(work / filename))
            fit["model_path"] = str((destination / filename).resolve())
        shutil.copyfile(work / "placement_000.cif", work / "aligned_model.cif")
        result = {"ok": True, "aligner": "VESPER", "executable": binary,
                  "source_model": str(model), "map_path": str(density), "candidate_index": candidate_index,
                  "source_model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                  "aligned_model": str((destination / "aligned_model.cif").resolve()),
                  "rotation_translation_matrix": fits[0]["rotation_translation_matrix"],
                  "dot_score": fits[0]["dot_score"], "placements": fits,
                  "model_handedness_changed": False, "coordinate_frame": "input map physical coordinates",
                  "settings": {"resolution_A": resolution, "contour": contour, "grid_spacing_A": grid_spacing,
                               "angular_step_degrees": angular_step, "top_n": top_n, "bandwidth": bandwidth, "cores": cores},
                  "command": command, "note": "DOT ranks VESPER placements; it is not a Q-score or a handedness probability."}
        result["result_path"] = str((destination / "fit_result.json").resolve())
        (work / "fit_result.json").write_text(json.dumps(result, indent=2, allow_nan=False))
        files = list(work.iterdir())
        if any((destination / path.name).resolve() in (model.resolve(), density.resolve()) for path in files):
            raise ValueError("Alignment outputs must not overwrite input files.")
        for path in files:
            shutil.copyfile(path, destination / path.name)
    return result
