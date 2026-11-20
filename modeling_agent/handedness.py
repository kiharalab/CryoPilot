"""Compare DAQ on explicitly matched residues, never pooled candidates.

Comparisons use the centered 19-residue mean of AA DAQ, computed along each chain
over the residues scored in both tables, so both sides are smoothed over the same
neighbours."""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path


def model_provenance(path: Path, candidate_index: int = 1) -> dict:
    """Recognize placements derived from the same untransformed candidate."""
    fit_path = path.parent / "fit_result.json"
    if fit_path.exists():
        fit = json.loads(fit_path.read_text())
        placements = [fit.get("aligned_model"), *[p.get("model_path") for p in fit.get("placements", [])]]
        if str(path.resolve()) in placements and fit.get("source_model_sha256"):
            return {"source_sha256": fit["source_model_sha256"], "candidate_index": fit["candidate_index"]}
    return {"source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "candidate_index": candidate_index}


def _read_scores(path: Path):
    scores = {}
    candidates = set()
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = ["chain", "residue_number", "insertion_code", "residue_name"]
        if not set(fields + ["aa"]) <= set(reader.fieldnames or []):
            raise ValueError(f"Missing identity columns or raw AA DAQ scores in {path}.")
        for row in reader:
            key = tuple(row[field].strip() for field in fields)
            if key in scores:
                raise ValueError("Duplicate residue identity; compare one candidate at a time.")
            if row.get("candidate_index"):
                candidates.add(row["candidate_index"])
            value = float(row["aa"]) if row["aa"] else None
            scores[key] = value if value is not None and math.isfinite(value) else None
    if len(candidates) > 1 or not scores:
        raise ValueError("Expected one nonempty candidate score table.")
    return scores


DAQ_WINDOW = 19


def matched_window_means(left: dict, right: dict, window: int = DAQ_WINDOW):
    """Centered `window`-residue means along each chain over residues scored in both tables."""
    shared = [key for key in left if key in right and left[key] is not None and right[key] is not None]
    by_chain: dict[str, list] = {}
    for key in shared:
        by_chain.setdefault(key[0], []).append(key)
    half = window // 2
    smooth_left, smooth_right = {}, {}
    for keys in by_chain.values():
        for i, key in enumerate(keys):
            block = keys[max(0, i - half): i + half + 1]
            smooth_left[key] = sum(left[k] for k in block) / len(block)
            smooth_right[key] = sum(right[k] for k in block) / len(block)
    return shared, smooth_left, smooth_right


def compare_scores(original: Path, flipped: Path, output: Path, window: int = DAQ_WINDOW) -> dict:
    if output.suffix.lower() != ".json":
        raise ValueError("Comparison report path must end in .json.")
    if original.resolve() == flipped.resolve():
        raise ValueError("Provide distinct score tables for the two map hands.")
    metadata = [json.loads(path.with_suffix(".metadata.json").read_text()) for path in (original, flipped)]
    if any(m.get("metric_type") != "daq" for m in metadata):
        raise ValueError("Missing or incompatible score provenance; recompute using the agent tools.")
    if metadata[0]["settings"] != metadata[1]["settings"]:
        raise ValueError("Scoring settings differ between map hands; recompute with equivalent settings.")
    if not metadata[0].get("reference") or metadata[0]["reference"] != metadata[1].get("reference"):
        raise ValueError("Scores come from different source candidates; independently fit the SAME model to both hands.")
    if metadata[0]["map_path"] == metadata[1]["map_path"]:
        raise ValueError("Both score tables refer to the same map.")
    left, right = _read_scores(original), _read_scores(flipped)
    if left.keys() != right.keys():
        raise ValueError("Residue identities differ; use the same candidate in both hands.")
    shared, left_used, right_used = matched_window_means(left, right, window)
    if not shared:
        raise ValueError("No jointly scored residues; handedness is unresolved.")
    original_mean = sum(left_used[key] for key in shared) / len(shared)
    flipped_mean = sum(right_used[key] for key in shared) / len(shared)
    delta = flipped_mean - original_mean
    result = {"ok": True, "metric": f"aa_{window}_residue_mean", "original_map": metadata[0]["map_path"],
              "flipped_map": metadata[1]["map_path"], "original_scores": str(original), "flipped_scores": str(flipped),
              "original_mean": original_mean, "flipped_mean": flipped_mean, "delta_flipped_minus_original": delta,
              "flipped_higher": delta > 0,
              "coverage": {"total": len(left), "original_scored": sum(v is not None for v in left.values()),
                           "flipped_scored": sum(v is not None for v in right.values()), "matched": len(shared),
                           "excluded_from_both_means": len(left) - len(shared)},
              "settings": metadata[0]["settings"], "reference": metadata[0]["reference"],
              "note": f"DAQ means average the {window}-residue mean over residues scored in both hands. Means use only the explicit matched coverage. This is one line of evidence about the hand; weigh it with coverage, the size of the difference and the overlays."}
    if output.resolve() in (original.resolve(), flipped.resolve()):
        raise ValueError("Comparison output must not overwrite scores.")
    output.parent.mkdir(parents=True, exist_ok=True)
    paired = output.with_suffix(".csv")
    if paired.resolve() in (original.resolve(), flipped.resolve()):
        raise ValueError("Comparison output must not overwrite scores.")
    with paired.open("w", newline="") as handle:
        writer = csv.writer(handle)
        mean_label = f"{window}_residue_mean"
        writer.writerow(["identity", "original_single_residue", "flipped_single_residue",
                         f"original_{mean_label}", f"flipped_{mean_label}", "used_in_both_means"])
        for key in left:
            used = key in left_used and key in right_used and left[key] is not None and right[key] is not None
            writer.writerow([json.dumps(key), left[key], right[key], left_used.get(key) if used else "",
                             right_used.get(key) if used else "", used])
    result["paired_scores"] = str(paired.resolve())
    result["report_path"] = str(output.resolve())
    output.write_text(json.dumps(result, indent=2, allow_nan=False))
    return result


def compare_stage_scores(before: Path, after: Path, output: Path, window: int = DAQ_WINDOW) -> dict:
    """Compare two versions of one model against the same map by windowed-mean AA DAQ.

    Residue sets may differ (for example after trimming); only residues scored in both
    tables are used, and the 19-residue mean is taken along each chain over those
    residues in both tables.
    """
    if output.suffix.lower() != ".json":
        raise ValueError("Comparison report path must end in .json.")
    metadata = [json.loads(path.with_suffix(".metadata.json").read_text()) for path in (before, after)]
    if any(m.get("metric_type") != "daq" for m in metadata):
        raise ValueError("Missing or incompatible DAQ provenance; recompute with run_daq.")
    if metadata[0]["settings"] != metadata[1]["settings"]:
        raise ValueError("DAQ settings differ between stages; recompute with equivalent settings.")
    if metadata[0]["map_path"] != metadata[1]["map_path"]:
        raise ValueError("The two versions were scored against different maps; score both against the same map.")
    left, right = _read_scores(before), _read_scores(after)
    shared, left_used, right_used = matched_window_means(left, right, window)
    if not shared:
        raise ValueError("No residues scored in both stages.")
    before_mean = sum(left_used[k] for k in shared) / len(shared)
    after_mean = sum(right_used[k] for k in shared) / len(shared)
    raw_before = sum(left[k] for k in shared) / len(shared)
    raw_after = sum(right[k] for k in shared) / len(shared)
    result = {"ok": True, "metric": f"aa_{window}_residue_mean", "map_path": metadata[0]["map_path"],
              "before_scores": str(before), "after_scores": str(after),
              "before_mean": before_mean, "after_mean": after_mean, "delta_after_minus_before": after_mean - before_mean,
              "raw_before_mean": raw_before, "raw_after_mean": raw_after,
              "coverage": {"before_residues": len(left), "after_residues": len(right), "matched": len(shared)}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False))
    result["report_path"] = str(output.resolve())
    return result
