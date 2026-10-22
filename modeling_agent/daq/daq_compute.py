#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from typing import Iterable

import numpy as np

# Number GPUs as nvidia-smi does (PCI bus order), so CUDA_VISIBLE_DEVICES=2 means nvidia-smi GPU 2.
# CUDA's default order is fastest first, which puts a small display GPU at a different index.
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from map_utils import read_mrc as _read_mrc, resample_to_1a
DAQ_CODE_ROOT = REPO_ROOT / "vendor" / "daqcore"
DAQ_SRC = DAQ_CODE_ROOT / "daqcolor" / "src"
DAQ_MODELS_DIR = REPO_ROOT / "models"
DAQ_SYNTHETIC_PACKAGE = "daqweb_daqcolor_src"
if str(DAQ_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(DAQ_CODE_ROOT))

os.environ.setdefault("DAQ_MODEL_PATH", str(DAQ_MODELS_DIR / "Multimodel.onnx"))
os.environ.setdefault("DAQ_MLX_WEIGHTS", str(DAQ_MODELS_DIR / "Multimodel.mlx.npz"))


def _load_daq_module(module_name: str):
    package = sys.modules.get(DAQ_SYNTHETIC_PACKAGE)
    if package is None:
        spec = importlib.util.spec_from_loader(DAQ_SYNTHETIC_PACKAGE, loader=None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(DAQ_SRC)]
        sys.modules[DAQ_SYNTHETIC_PACKAGE] = package

    full_name = f"{DAQ_SYNTHETIC_PACKAGE}.{module_name}"
    existing = sys.modules.get(full_name)
    if existing is not None:
        return existing

    module_path = DAQ_SRC / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(full_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load DAQ module {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module

AA20 = [
    "ALA", "VAL", "PHE", "PRO", "MET", "ILE", "LEU", "ASP", "GLU", "LYS",
    "ARG", "SER", "THR", "TYR", "HIS", "CYS", "ASN", "TRP", "GLN", "GLY",
]
AA_INDEX = {aa: i for i, aa in enumerate(AA20)}
ATOM_TYPES6 = ["Other", "N", "CA", "C", "O", "CB"]


def _compute_grid_from_map(args, output_path: Path):
    compute = _load_daq_module("compute")
    onnx_model = _load_daq_module("onnx_model")

    data, origin, step = _read_mrc(args.map)
    original_step = step
    data, origin, step = resample_to_1a(data, origin, step)
    data_norm = compute.normalize_volume(data)
    points, density = compute.extract_threshold_points(
        data,
        origin,
        step,
        contour=float(args.contour) * 0.5,
        stride=int(args.stride),
        max_points=int(args.max_points) if int(args.max_points) > 0 else None,
    )
    if points.shape[0] == 0:
        raise RuntimeError("No map points were found above the contour threshold.")

    patches = compute.extract_patches(data_norm, points, origin, step, patch_size=11)
    model = onnx_model.load_model(args.model_path, backend=args.backend, gpu_id=int(args.gpu_id))
    aa_probs, atom_probs, ss_probs = model.predict_batched(
        patches,
        batch_size=int(args.batch_size),
        progress_callback=None,
    )
    scores = compute.compute_log_ratio_scores(points, aa_probs, atom_probs, ss_probs, density, float(args.contour))
    np.save(output_path, scores)
    output_path.with_suffix(".json").write_text(json.dumps({
        "schema_version": 1, "source_map": str(args.map.resolve()),
        "input_spacing_A": original_step, "inference_spacing_A": step,
        "origin_A": origin, "contour": args.contour, "stride": args.stride,
        "max_points": args.max_points, "seed": args.seed,
        "reference": "mean prediction over map points above contour",
        "ss_assignment": "C-alpha distance heuristic (not DSSP)",
    }, indent=2))
    return output_path


def _load_grid(path: Path):
    arr = np.load(path)
    if arr.ndim != 2 or arr.shape[1] != 32:
        raise ValueError(f"Expected DAQ grid with shape (N,32); got {arr.shape}.")
    if not len(arr) or not np.isfinite(arr).all():
        raise ValueError("DAQ grid must contain finite coordinates and scores.")
    return (
        np.ascontiguousarray(arr[:, :3], dtype=np.float32),
        np.ascontiguousarray(arr[:, 3:23], dtype=np.float32),
        np.ascontiguousarray(arr[:, 23:29], dtype=np.float32),
        np.ascontiguousarray(arr[:, 29:32], dtype=np.float32),
    )


def _knn_features(pts: np.ndarray, feat: np.ndarray, q: np.ndarray, k: int, radius: float | None):
    try:
        from scipy.spatial import cKDTree
    except Exception as exc:
        raise RuntimeError("Missing Python dependency 'scipy'. Install it to assign DAQ scores to residues.") from exc

    tree = cKDTree(pts)
    kwargs = {}
    if radius is not None:
        kwargs["distance_upper_bound"] = radius
    dist, idx = tree.query(q, k=k, **kwargs)
    if k == 1:
        dist = dist[:, None]
        idx = idx[:, None]

    valid = (idx >= 0) & (idx < pts.shape[0]) & np.isfinite(dist)
    best_pos = np.argmin(dist, axis=1)
    rows = np.arange(q.shape[0])
    has_neighbor = valid.any(axis=1)
    best_is_valid = has_neighbor & valid[rows, best_pos]
    safe_idx = np.zeros(q.shape[0], dtype=np.int64)
    safe_idx[best_is_valid] = idx[rows, best_pos][best_is_valid]

    out = feat[safe_idx].copy()
    out[~best_is_valid] = 0.0
    return out, has_neighbor


def _read_structure(path: Path):
    try:
        import gemmi
    except Exception as exc:
        raise RuntimeError("Missing Python dependency 'gemmi'. Install it to read and write PDB/mmCIF files.") from exc

    structure = gemmi.read_structure(str(path))
    return structure


def _protein_residues(structure):
    if len(structure) != 1:
        raise ValueError("Score one candidate model at a time.")
    residues = []
    chain_ids = []
    resnums = []
    names = []
    icodes = []
    atoms_by_res = []

    for model in structure:
        for chain in model:
            for residue in chain:
                name = (residue.name or "").upper()
                if name not in AA_INDEX or len(residue) == 0:
                    continue
                residues.append(residue)
                chain_ids.append(chain.name)
                resnums.append(int(residue.seqid.num) if residue.seqid.num is not None else 0)
                names.append(name)
                icodes.append(residue.seqid.icode.strip())
                atoms_by_res.append([atom for atom in residue])

    if not residues:
        raise RuntimeError("No standard amino-acid residues found in the model.")

    return residues, np.array(chain_ids, dtype=object), np.array(resnums, dtype=np.int64), icodes, names, atoms_by_res


def _residue_coords(atoms_by_res, atom_name: str):
    coords = []
    for atoms in atoms_by_res:
        chosen = next((atom for atom in atoms if (atom.name or "").strip().upper() == atom_name.upper()), None)
        if chosen is not None:
            coords.append((float(chosen.pos.x), float(chosen.pos.y), float(chosen.pos.z)))
            continue

        # Missing C-alpha is unscored, not silently replaced by an atom centroid.
        coords.append((np.nan, np.nan, np.nan))
    return np.asarray(coords, dtype=np.float32)


def _window_average(chain_ids: np.ndarray, resnums: np.ndarray, values: np.ndarray, half_window: int):
    if half_window <= 0:
        return values

    out = np.full(values.shape, np.nan, dtype=np.float32)
    for chain in np.unique(chain_ids):
        chain_idx = np.where(chain_ids == chain)[0]
        nums = resnums[chain_idx]
        order = np.argsort(nums, kind="stable")
        sorted_idx = chain_idx[order]
        sorted_nums = nums[order]
        sorted_values = values[sorted_idx]
        finite = np.isfinite(sorted_values)
        safe_values = np.where(finite, sorted_values, 0.0)
        prefix_sum = np.concatenate(([0.0], np.cumsum(safe_values, dtype=np.float64)))
        prefix_count = np.concatenate(([0], np.cumsum(finite.astype(np.int64))))
        left = np.searchsorted(sorted_nums, sorted_nums - half_window, side="left")
        right = np.searchsorted(sorted_nums, sorted_nums + half_window, side="right")
        counts = prefix_count[right] - prefix_count[left]
        sums = prefix_sum[right] - prefix_sum[left]
        valid = counts > 0
        out[sorted_idx[valid]] = (sums[valid] / counts[valid]).astype(np.float32)
    out[~np.isfinite(values)] = np.nan
    return out


def _infer_ss_indices(q: np.ndarray, chain_ids: np.ndarray, resnums: np.ndarray):
    # 0 helix, 1 strand, 2 coil. This mirrors the web UI fallback when DSSP is unavailable.
    ss = np.full(q.shape[0], 2, dtype=np.int64)
    for i in range(q.shape[0]):
        for j, target_delta, min_dist, max_dist, value in ((i + 4, 4, 4.8, 6.8, 0), (i + 2, 2, 6.2, 8.8, 1)):
            if j >= q.shape[0] or chain_ids[i] != chain_ids[j] or resnums[j] - resnums[i] != target_delta:
                continue
            dist = float(np.linalg.norm(q[i] - q[j]))
            if min_dist <= dist <= max_dist:
                ss[i : j + 1] = np.where(ss[i : j + 1] == 2, value, ss[i : j + 1])
    return ss


def _metric_scores(metric: str, aa_mean: np.ndarray, atom_mean: np.ndarray, ss_mean: np.ndarray, resnames: list[str], ss_idx: np.ndarray):
    if metric == "aa":
        idx = np.array([AA_INDEX.get(name, -1) for name in resnames], dtype=np.int64)
        scal = np.full(idx.shape, np.nan, dtype=np.float32)
        valid = idx >= 0
        rows = np.nonzero(valid)[0]
        scal[rows] = aa_mean[rows, idx[valid]]
        return scal
    if metric == "ca":
        return atom_mean[:, ATOM_TYPES6.index("CA")].astype(np.float32)
    if metric == "ss":
        return ss_mean[np.arange(ss_mean.shape[0]), ss_idx].astype(np.float32)
    if metric == "total":
        parts = np.stack(
            [
                _metric_scores("aa", aa_mean, atom_mean, ss_mean, resnames, ss_idx),
                _metric_scores("ca", aa_mean, atom_mean, ss_mean, resnames, ss_idx),
                _metric_scores("ss", aa_mean, atom_mean, ss_mean, resnames, ss_idx),
            ],
            axis=1,
        )
        with np.errstate(invalid="ignore"):
            return np.nanmean(parts, axis=1).astype(np.float32)
    raise ValueError(f"Unknown metric {metric}")


def _write_scores_to_pdb(candidate, output_path: Path, scores: Iterable[float], nan_fill: float):
    structure = candidate.clone()
    _, _, _, _, _, atoms_by_res = _protein_residues(structure)
    for atoms, score in zip(atoms_by_res, scores):
        value = float(score) if math.isfinite(float(score)) else nan_fill
        for atom in atoms:
            atom.b_iso = value
            if not math.isfinite(float(score)):
                atom.occ = 0.0  # Distinguish unscored residues from neutral DAQ.
    structure.write_pdb(str(output_path))
    structure.make_mmcif_document().write_file(str(output_path.with_suffix(".cif")))
    return output_path


def _write_score_table(
    output_path: Path,
    chain_ids: np.ndarray,
    resnums: np.ndarray,
    icodes: list[str],
    resnames: list[str],
    metric_values: dict[str, np.ndarray],
    smoothed: dict[str, np.ndarray],
    candidate_index: int,
    model_number: int,
    coverage: np.ndarray,
):
    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["candidate_index", "model_number", "index", "chain", "residue_number", "insertion_code", "residue_name", "scored", "aa", "ca", "ss", "total", "aa_smoothed", "ca_smoothed", "ss_smoothed", "total_smoothed"])
        for index, (chain, resnum, resname) in enumerate(zip(chain_ids, resnums, resnames), start=1):
            writer.writerow(
                [
                    candidate_index, model_number, index,
                    chain,
                    int(resnum),
                    icodes[index - 1],
                    resname,
                    bool(coverage[index - 1]),
                    *[
                        "" if not np.isfinite(metric_values[name][index - 1]) else float(metric_values[name][index - 1])
                        for name in ("aa", "ca", "ss", "total")
                    ],
                    *["" if not np.isfinite(smoothed[name][index - 1]) else float(smoothed[name][index - 1])
                      for name in ("aa", "ca", "ss", "total")],
                ]
            )
    return output_path


def _plot_scores(output_path: Path, metric_values, smoothed, chain_ids, resnums, icodes, candidate_index, half_window):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chains = list(dict.fromkeys(chain_ids))
    figure, axes = plt.subplots(len(chains), 3, squeeze=False, figsize=(16, 3 * len(chains)), constrained_layout=True)
    for row, chain in enumerate(chains):
        indices = np.where(chain_ids == chain)[0]
        indices = np.array(sorted(indices, key=lambda i: (resnums[i], icodes[i])))
        # Numeric residue positions with small offsets for insertion codes.
        seen = {}
        x = []
        for i in indices:
            number = int(resnums[i])
            offset = seen.get(number, 0)
            x.append(number + offset * 0.1)
            seen[number] = offset + 1
        x = np.array(x)
        for col, metric in enumerate(("aa", "ca", "ss")):
            axis = axes[row, col]
            values = metric_values[metric][indices]
            axis.plot(x, values, color="#64748b", linewidth=0.7, alpha=0.65, label="raw")
            axis.scatter(x, values, c=values, cmap="RdBu", vmin=-1.5, vmax=1.5, s=7)
            if half_window:
                axis.plot(x, smoothed[metric][indices], color="#2563eb", linewidth=1.4,
                          label=f"smoothed ±{half_window} residue numbers")
            missing = ~np.isfinite(values)
            if missing.any():
                axis.scatter(x[missing], np.zeros(missing.sum()), marker="x", color="#111827", s=15, label="unscored")
            ticks = np.unique(np.linspace(0, len(indices) - 1, min(7, len(indices))).astype(int))
            axis.set_xticks(x[ticks], [f"{resnums[indices[t]]}{icodes[indices[t]]}" for t in ticks])
            axis.axhline(0, color="#94a3b8", linestyle="--", linewidth=0.8)
            axis.set(title=f"Chain {chain} · {metric.upper()}", xlabel="Residue number (insertion code retained)", ylabel="DAQ log-ratio")
            axis.grid(axis="y", alpha=0.15)
            axis.spines[["top", "right"]].set_visible(False)
            axis.legend(frameon=False, fontsize=8)
    figure.suptitle(f"Candidate {candidate_index} · residue-wise DAQ (red low / blue high)", fontsize=15)
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize
    figure.colorbar(ScalarMappable(norm=Normalize(-1.5, 1.5), cmap="RdBu"), ax=axes.ravel().tolist(),
                    label="DAQ color scale (clipped at ±1.5)", shrink=0.6)
    figure.savefig(output_path, dpi=150, facecolor="white")
    plt.close(figure)
    return output_path


def _score_candidate(candidate, candidate_index, pts, aa, atom, ss3, args):
    _, chain_ids, resnums, icodes, resnames, atoms_by_res = _protein_residues(candidate)
    q = _residue_coords(atoms_by_res, "CA")
    valid_coords = np.isfinite(q).all(axis=1)
    features = []
    coverage = valid_coords.copy()
    for feat in (aa, atom, ss3):
        sampled = np.zeros((len(q), feat.shape[1]), dtype=np.float32)
        if valid_coords.any():
            sampled[valid_coords], has = _knn_features(pts, feat, q[valid_coords], 1, args.radius if args.radius > 0 else None)
            coverage[valid_coords] &= has
        features.append(sampled)
    ss_idx = _infer_ss_indices(q, chain_ids, resnums)
    raw, smoothed, summaries, scored = {}, {}, {}, {}
    destination = args.output_dir / f"candidate_{candidate_index:03d}"
    destination.mkdir(parents=True, exist_ok=True)
    inputs = {args.model.resolve()}
    if args.map:
        inputs.add(args.map.resolve())
    generated = [destination / f"{metric}.{suffix}" for metric in ("aa", "ca", "ss", "total") for suffix in ("pdb", "cif")]
    if any(path.resolve() in inputs for path in generated):
        raise ValueError("DAQ outputs must not overwrite the input model or map.")
    for metric in ("aa", "ca", "ss", "total"):
        values = _metric_scores(metric, *features, resnames, ss_idx)
        values[~coverage] = np.nan
        raw[metric] = values
        smoothed[metric] = _window_average(chain_ids, resnums, values, args.half_window)
        scored[metric] = str(_write_scores_to_pdb(candidate, destination / f"{metric}.pdb", values, args.nan_fill).resolve())
        finite = values[np.isfinite(values)]
        summaries[metric] = {"count": len(values), "finite": len(finite),
                             "min": float(finite.min()) if len(finite) else None,
                             "max": float(finite.max()) if len(finite) else None,
                             "mean": float(finite.mean()) if len(finite) else None}
    table = _write_score_table(destination / "daq_scores.csv", chain_ids, resnums, icodes, resnames,
                              raw, smoothed, candidate_index, candidate[0].num, coverage)
    from handedness import model_provenance
    table.with_suffix(".metadata.json").write_text(json.dumps({
        "metric_type": "daq", "map_path": str(args.map.resolve()) if args.map else None,
        "model_path": str(args.model.resolve()), "candidate_index": candidate_index,
        "reference": model_provenance(args.model, candidate_index),
        "settings": {"contour": args.contour, "stride": args.stride, "max_points": args.max_points,
                     "seed": args.seed, "backend": args.backend, "model_path": args.model_path,
                     "radius": args.radius, "inference_spacing_A": 1.0, "scores": "raw",
                     "normalization": "sampled map points", "ss_assignment": "C-alpha heuristic"},
    }, indent=2))
    plot = _plot_scores(destination / "daq_scores.png", raw, smoothed, chain_ids, resnums, icodes, candidate_index, args.half_window)
    return {"candidate_index": candidate_index, "model_number": candidate[0].num, "summary": summaries,
            "coverage": {"scored": int(coverage.sum()), "total": len(coverage)},
            "scoredPdbs": scored, "scoredCifs": {key: str(Path(value).with_suffix('.cif')) for key, value in scored.items()},
            "scoreTable": str(table.resolve()), "scorePlot": str(plot.resolve())}


def main():
    parser = argparse.ArgumentParser(description="DAQweb local DAQ compute bridge")
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--map", type=Path)
    parser.add_argument("--grid", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model-path")
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--gpu-id", default=0, type=int)
    parser.add_argument("--contour", default=0.0, type=float)
    parser.add_argument("--stride", default=1, type=int)
    parser.add_argument("--max-points", default=0, type=int, help="0 keeps every point; a cap subsamples at random")
    parser.add_argument("--batch-size", default=0, type=int)
    parser.add_argument("--radius", default=3.0, type=float)
    parser.add_argument("--half-window", default=0, type=int)
    parser.add_argument("--candidate-index", default=0, type=int, help="1-based candidate; 0 scores all separately")
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--nan-fill", default=0.0, type=float)
    args = parser.parse_args()
    if args.half_window < 0 or args.candidate_index < 0 or not np.isfinite(args.contour):
        raise ValueError("Invalid smoothing window, candidate index, or contour.")

    np.random.seed(int(args.seed))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    grid_path = args.grid
    if grid_path is None:
        if args.map is None:
            raise RuntimeError("Provide either a cryo-EM map or an existing DAQ grid .npy file.")
        grid_path = args.output_dir / "daq_grid.npy"
        _compute_grid_from_map(args, grid_path)

    pts, aa, atom, ss3 = _load_grid(grid_path)
    structure = _read_structure(args.model)
    if not len(structure) or args.candidate_index > len(structure):
        raise ValueError(f"Candidate index must be 1..{len(structure)}, or 0 for all.")
    candidates = []
    for index in range(len(structure)):
        if args.candidate_index and args.candidate_index != index + 1:
            continue
        candidate = structure.clone()
        for remove in reversed(range(len(candidate))):
            if remove != index:
                del candidate[remove]
        candidates.append(_score_candidate(candidate, index + 1, pts, aa, atom, ss3, args))
    result = {"ok": True, "schema_version": 2, "gridPath": str(grid_path.resolve()),
              "candidates": candidates, "half_window": args.half_window,
              "notes": ["Summaries and unsuffixed CSV scores are raw, candidate-specific log-ratios.",
                        "SS uses a C-alpha distance heuristic, not DSSP.",
                        "total is an arithmetic average, not an independently calibrated metric.",
                        "Unscored PDB/mmCIF atoms have occupancy zero; consult CSV coverage."]}
    if len(candidates) == 1:
        result.update({key: candidates[0][key] for key in ("scoredPdbs", "scoreTable", "scorePlot", "summary")})
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)
