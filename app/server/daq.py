"""DAQ score tables written by the modeling agent's ``run_daq`` tool.

For each scored candidate, ``modeling_agent/daq/daq_compute.py`` writes
``candidate_NNN/daq_scores.csv`` (raw per-residue AA, CA, SS and total
scores), a ``daq_scores.metadata.json`` sidecar (model, map, contour), and
``aa.pdb``/``ca.pdb``/... with the raw score in the B-factor column. The app
reads these files to plot per-residue profiles and to colour the model by
score in Mol*.

The headline number is the centered 19-residue mean of AA DAQ along each
chain, computed like ``modeling_agent/refinement.py::_running_mean``: the
window counts residues in file order, averages only the scored ones in it,
and is empty for a residue that is itself unscored.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .files import SKIP_DIRS, confine

SCORE_TABLE_NAME = "daq_scores.csv"
METADATA_NAME = "daq_scores.metadata.json"
SCORED_MODEL_NAME = "aa.pdb"
METRICS = ("aa", "ca", "ss", "total")
STRUCTURE_METRICS = ("aa_mean", *METRICS)
DEFAULT_WINDOW = 19
MAX_WINDOW = 501
MAX_SEARCH_DEPTH = 8
MAX_SEARCH_DIRS = 20_000
PARSE_CACHE_SIZE = 64


class DaqError(ValueError):
    pass


# ------------------------------------------------------------------ parsing
def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def read_metadata(table: Path) -> dict[str, Any]:
    """The sidecar written next to a score table, or {} when it is missing or unreadable."""

    path = table.with_name(METADATA_NAME)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_rows(table: Path, candidate_index: Any) -> list[dict[str, Any]]:
    with table.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        if not {"chain", "residue_number", "aa"} <= fields:
            raise DaqError("This file is not a DAQ score table")
        raw_rows = list(reader)

    # One table holds one candidate; if several were concatenated, keep the one the metadata names.
    candidates = {str(row.get("candidate_index") or "").strip() for row in raw_rows} - {""}
    if len(candidates) > 1:
        wanted = str(candidate_index or "").strip() or min(candidates, key=lambda c: (len(c), c))
        raw_rows = [row for row in raw_rows if str(row.get("candidate_index") or "").strip() == wanted]

    rows = []
    for position, row in enumerate(raw_rows, start=1):
        try:
            resnum = int(str(row.get("residue_number") or "").strip())
        except ValueError:
            continue
        values = {metric: _number(row.get(metric)) for metric in METRICS}
        flagged = _flag(row["scored"]) if "scored" in fields else True
        index = str(row.get("index") or "").strip()
        rows.append(
            {
                "index": int(index) if index.isascii() and index.isdigit() else position,
                "chain": str(row.get("chain") or "").strip(),
                "resnum": resnum,
                "icode": str(row.get("insertion_code") or "").strip(),
                "resname": str(row.get("residue_name") or "").strip(),
                "scored": flagged and values["aa"] is not None,
                **values,
            }
        )
    return rows


_cache: OrderedDict[tuple[str, int, int, str], list[dict[str, Any]]] = OrderedDict()
_cache_lock = threading.Lock()


def load_rows(table: Path, candidate_index: Any = None) -> list[dict[str, Any]]:
    """Parsed rows of a score table, cached by path, size, and modification time."""

    stat = table.stat()
    key = (str(table), stat.st_mtime_ns, stat.st_size, str(candidate_index or ""))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    rows = _parse_rows(table, candidate_index)
    with _cache_lock:
        _cache[key] = rows
        while len(_cache) > PARSE_CACHE_SIZE:
            _cache.popitem(last=False)
    return rows


# ------------------------------------------------------------------ scores
def window_means(values: list[float | None], window: int) -> list[float | None]:
    """Centered running mean over ``window`` positions, ignoring missing values."""

    half = window // 2
    sums = [0.0]
    counts = [0]
    for value in values:
        sums.append(sums[-1] + (value if value is not None else 0.0))
        counts.append(counts[-1] + (value is not None))
    means: list[float | None] = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        count = counts[hi] - counts[lo]
        means.append((sums[hi] - sums[lo]) / count if count else None)
    return means


def _stats(values: list[float | None]) -> dict[str, Any]:
    finite = [value for value in values if value is not None]
    if not finite:
        return {"mean": None, "min": None, "max": None, "count": 0, "fraction_negative": None}
    return {
        "mean": sum(finite) / len(finite),
        "min": min(finite),
        "max": max(finite),
        "count": len(finite),
        "fraction_negative": sum(value < 0 for value in finite) / len(finite),
    }


def check_window(window: int) -> int:
    if not 1 <= int(window) <= MAX_WINDOW:
        raise DaqError(f"The window must be between 1 and {MAX_WINDOW} residues")
    return int(window)


def score_residues(rows: list[dict[str, Any]], window: int = DEFAULT_WINDOW) -> list[dict[str, Any]]:
    """Rows with ``aa_mean``: the windowed AA mean along each chain, in file order."""

    window = check_window(window)
    by_chain: dict[str, list[int]] = {}
    for position, row in enumerate(rows):
        by_chain.setdefault(row["chain"], []).append(position)
    aa_mean: list[float | None] = [None] * len(rows)
    for positions in by_chain.values():
        values = [rows[p]["aa"] if rows[p]["scored"] else None for p in positions]
        for position, mean in zip(positions, window_means(values, window)):
            aa_mean[position] = mean if rows[position]["scored"] else None
    residues = []
    for row, mean in zip(rows, aa_mean):
        residue = dict(row)
        if not residue["scored"]:
            residue.update({metric: None for metric in METRICS})
        residue["aa_mean"] = mean
        residues.append(residue)
    return residues


def summarize(residues: list[dict[str, Any]], window: int) -> dict[str, Any]:
    metrics = {metric: _stats([residue[metric] for residue in residues]) for metric in (*METRICS, "aa_mean")}
    return {
        "residues": len(residues),
        "scored": sum(residue["scored"] for residue in residues),
        "window": window,
        "mean": metrics["aa_mean"]["mean"],
        "fraction_negative": metrics["aa_mean"]["fraction_negative"],
        "metrics": metrics,
    }


def chain_summaries(residues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chains: dict[str, list[dict[str, Any]]] = {}
    for residue in residues:
        chains.setdefault(residue["chain"], []).append(residue)
    summaries = []
    for chain, members in chains.items():
        stats = _stats([residue["aa_mean"] for residue in members])
        summaries.append(
            {
                "chain": chain,
                "residues": len(members),
                "scored": sum(residue["scored"] for residue in members),
                "first": members[0]["resnum"],
                "last": members[-1]["resnum"],
                "mean": stats["mean"],
                "fraction_negative": stats["fraction_negative"],
            }
        )
    return summaries


# ---------------------------------------------------------------- results
def _relative_to(base: Path, recorded: Any) -> str | None:
    """A path recorded by the agent, relative to ``base`` when it lies inside it."""

    if not recorded:
        return None
    try:
        return Path(str(recorded)).resolve().relative_to(base).as_posix()
    except (ValueError, OSError):
        return None


def find_tables(workspace: Path) -> list[Path]:
    """Every DAQ score table under the workspace, without descending into huge trees."""

    workspace = workspace.resolve()
    if not workspace.is_dir():
        return []
    found: list[Path] = []
    base_depth = len(workspace.parts)
    for visited, (dirpath, dirnames, filenames) in enumerate(os.walk(workspace), start=1):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [] if depth >= MAX_SEARCH_DEPTH else sorted(
            name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")
        )
        if SCORE_TABLE_NAME in filenames:
            table = Path(dirpath) / SCORE_TABLE_NAME
            try:
                table.resolve().relative_to(workspace)
            except ValueError:
                pass  # a link to a table outside the workspace
            else:
                found.append(table)
        if visited >= MAX_SEARCH_DIRS:
            break
    return found


def describe(workspace: Path, table: Path, window: int = DEFAULT_WINDOW) -> dict[str, Any]:
    """One entry of the results list: where the table is, what it scored, and its summary."""

    workspace = workspace.resolve()
    metadata = read_metadata(table)
    settings = metadata.get("settings") if isinstance(metadata.get("settings"), dict) else {}
    candidate_index = metadata.get("candidate_index")
    residues = score_residues(load_rows(table, candidate_index), window)
    model_path = metadata.get("model_path")
    folder = table.parent.relative_to(workspace).as_posix()
    return {
        "path": table.relative_to(workspace).as_posix(),
        "folder": "" if folder == "." else folder,
        "label": Path(str(model_path)).name if model_path else table.parent.name,
        "model_path": model_path,
        "map_path": metadata.get("map_path"),
        "model_relative": _relative_to(workspace, model_path),
        "map_relative": _relative_to(workspace, metadata.get("map_path")),
        "structure_available": scored_model_path(workspace, table, metadata, required=False) is not None,
        "contour": settings.get("contour"),
        "candidate_index": candidate_index,
        "residue_count": len(residues),
        "scored_count": sum(residue["scored"] for residue in residues),
        "modified": table.stat().st_mtime,
        "summary": summarize(residues, window),
    }


def list_results(workspace: Path, window: int = DEFAULT_WINDOW) -> list[dict[str, Any]]:
    results = []
    for table in find_tables(workspace):
        try:
            results.append(describe(workspace, table, window))
        except (OSError, ValueError, csv.Error):  # ValueError covers DaqError and undecodable text
            continue  # a table being written right now, or not a DAQ table after all
    results.sort(key=lambda result: result["modified"], reverse=True)
    return results


def table_path(workspace: Path, relative: str) -> Path:
    """A score table named by its path relative to the workspace, confined to it."""

    table = confine(workspace.resolve(), relative)
    if not table.is_file():
        raise FileNotFoundError("DAQ score table not found")
    if table.suffix.lower() != ".csv":
        raise DaqError("Choose a DAQ score table (daq_scores.csv)")
    return table


def scores(workspace: Path, relative: str, window: int = DEFAULT_WINDOW) -> dict[str, Any]:
    table = table_path(workspace, relative)
    metadata = read_metadata(table)
    residues = score_residues(load_rows(table, metadata.get("candidate_index")), window)
    chains = list(dict.fromkeys(residue["chain"] for residue in residues))
    return {
        "path": table.relative_to(workspace.resolve()).as_posix(),
        "window": window,
        "chains": chains,
        "chain_summary": chain_summaries(residues),
        "residues": residues,
        "summary": summarize(residues, window),
        "metadata": metadata,
    }


# --------------------------------------------------------------- structure
def scored_model_path(
    workspace: Path,
    table: Path,
    metadata: dict[str, Any],
    *,
    required: bool = True,
) -> Path | None:
    """The model the table scored: ``aa.pdb`` next to it, else the metadata's PDB model.

    The metadata path is absolute (the agent resolves it); it is accepted only
    inside the project's modeling workspace, like every other DAQ path.
    """

    workspace = workspace.resolve()
    try:
        candidate = confine(workspace, table.with_name(SCORED_MODEL_NAME).relative_to(workspace).as_posix())
        if candidate.is_file():
            return candidate
    except ValueError:
        pass
    recorded = metadata.get("model_path")
    if recorded and str(recorded).lower().endswith((".pdb", ".ent")):
        path = Path(str(recorded)).resolve()
        try:
            path.relative_to(workspace)
        except ValueError:
            if required:
                raise DaqError("The scored model is outside this project's modeling workspace")
            return None
        if path.is_file():
            return path
    if required:
        raise FileNotFoundError("No PDB model was found next to this DAQ table")
    return None


def _pdb_chain(line: str) -> str:
    # Column 22 is the chain; gemmi writes two-character chain names into columns 21-22.
    return line[20:22].strip()


def _b_factor(value: float | None) -> str:
    if value is None:
        return "  0.00"
    return f"{max(-99.99, min(999.99, value)):6.2f}"


def rewrite_b_factors(pdb_text: str, values: dict[tuple[str, int, str], float | None]) -> str:
    """PDB text with each ATOM/HETATM B-factor (columns 61-66) set from ``values``."""

    lines = []
    for line in pdb_text.splitlines():
        if line.startswith(("ATOM  ", "HETATM")):
            try:
                key = (_pdb_chain(line), int(line[22:26]), line[26:27].strip())
            except ValueError:
                key = None
            padded = line.ljust(66)
            line = padded[:60] + _b_factor(values.get(key) if key else None) + padded[66:]
        lines.append(line)
    return "\n".join(lines) + "\n"


def scored_structure(
    workspace: Path,
    relative: str,
    metric: str = "aa_mean",
    window: int = DEFAULT_WINDOW,
) -> tuple[str, str]:
    """The scored model as PDB text with ``metric`` in the B-factor column, and a file name."""

    if metric not in STRUCTURE_METRICS:
        raise DaqError(f"Unknown DAQ metric: {metric}")
    table = table_path(workspace, relative)
    metadata = read_metadata(table)
    residues = score_residues(load_rows(table, metadata.get("candidate_index")), window)
    model = scored_model_path(workspace, table, metadata)
    values = {(residue["chain"], residue["resnum"], residue["icode"]): residue[metric] for residue in residues}
    text = model.read_text(encoding="utf-8", errors="replace")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(str(metadata.get("model_path") or model)).stem) or "model"
    return rewrite_b_factors(text, values), f"{stem}_daq_{metric}.pdb"
