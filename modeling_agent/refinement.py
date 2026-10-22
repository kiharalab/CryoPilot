"""Per-residue support, model editing, and real-space refinement with Phenix and Rosetta.

Executables are found from PHENIX_BIN / ROSETTA_BIN / ROSETTA3_DB, then PATH, then the
zebra install locations. Every external run writes its command and log to the output
directory so that each refinement stage can be audited and repeated.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ZEBRA_PHENIX_BIN = "/apps/phenix/phenix-2.2-6143/bin"
ZEBRA_ROSETTA_BIN = "/apps/rosetta/3.13/main/source/bin"
ZEBRA_ROSETTA_DB = "/apps/rosetta/3.13/main/database"
PHENIX_TIMEOUT = 6 * 3600
ROSETTA_TIMEOUT = 24 * 3600
CHAIN_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

PHENIX_RUN_OPTIONS = ("minimization_global", "rigid_body", "local_grid_search", "morphing",
                      "simulated_annealing", "adp", "occupancy", "nqh_flips")


# ------------------------------------------------------------------ per-residue support
def _running_mean(values, window):
    half = window // 2
    out = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - half): i + half + 1] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


def _read_daq(scores_csv: Path | None, candidate_index: int) -> dict:
    if not scores_csv:
        return {}
    with Path(scores_csv).open() as handle:
        rows = [r for r in csv.DictReader(handle)
                if not r.get("candidate_index") or int(r["candidate_index"]) == candidate_index]
    return {(r["chain"], int(r["residue_number"]), r["insertion_code"].strip()):
            float(r["aa"]) if r["scored"] == "True" and r["aa"] not in ("", "nan") else None for r in rows}


def residue_support(model: Path, density: Path, contour: float, output_csv: Path,
                    daq_scores: Path | None = None, candidate_index: int = 1, window: int = 19) -> dict:
    """Per-residue evidence that the map supports each residue, for the agent to interpret.

    For every residue: the fraction of its non-hydrogen atoms in density above `contour`, the
    mean map value at its atoms relative to the contour, the raw AA DAQ score and its centred
    `window`-residue mean (when a DAQ table is given), and whether the Cα–Cα distance to the
    previous residue in the chain exceeds 4.2 Å (a chain break or a long peptide link). Nothing
    is removed or changed; the summary lists contiguous runs of weakly supported residues so they
    can be inspected.
    """
    import gemmi
    import numpy as np
    from scipy.ndimage import map_coordinates

    from map_utils import read_mrc

    model, density, output_csv = Path(model).resolve(), Path(density).resolve(), Path(output_csv).resolve()
    structure = gemmi.read_structure(str(model))
    if len(structure) != 1:
        raise ValueError("Analyse one candidate model at a time.")
    data, origin, step = read_mrc(density)
    daq = _read_daq(daq_scores, candidate_index)
    rows = []
    for chain in structure[0]:
        previous_ca = None
        chain_rows = []
        for res in chain:
            atoms = [a for a in res if a.element.name not in ("H", "D")]
            if not atoms:
                continue
            xyz = np.array([[a.pos.x, a.pos.y, a.pos.z] for a in atoms])
            index = (xyz - np.asarray(origin)) / np.asarray(step)       # x, y, z voxel coordinates
            values = map_coordinates(data, index[:, ::-1].T, order=1, mode="constant", cval=0.0)
            ca = res.find_atom("CA", "*")
            gap = None
            if ca is not None and previous_ca is not None:
                gap = round(ca.pos.dist(previous_ca), 2)
            if ca is not None:
                previous_ca = ca.pos
            key = (chain.name, res.seqid.num, res.seqid.icode.strip())
            chain_rows.append({"chain": chain.name, "residue_number": res.seqid.num,
                               "insertion_code": res.seqid.icode.strip(), "residue_name": res.name,
                               "atoms": len(atoms), "fraction_above_contour": round(float((values > contour).mean()), 3),
                               "mean_density_over_contour": round(float(values.mean() / contour), 3) if contour else None,
                               "daq_aa": daq.get(key), "ca_gap_to_previous_A": gap,
                               "chain_break_before": bool(gap is not None and gap > 4.2)})
        smooth = _running_mean([r["daq_aa"] for r in chain_rows], window)
        for r, m in zip(chain_rows, smooth):
            r[f"daq_aa_mean_{window}"] = round(m, 4) if (m is not None and r["daq_aa"] is not None) else None
        rows.extend(chain_rows)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    def runs(flag):
        found, current = [], []
        for r in rows + [None]:
            if r is not None and flag(r) and (not current or current[-1]["chain"] == r["chain"]):
                current.append(r)
                continue
            if current:
                found.append({"chain": current[0]["chain"], "start": current[0]["residue_number"],
                              "end": current[-1]["residue_number"], "length": len(current),
                              "mean_fraction_above_contour": round(sum(x["fraction_above_contour"] for x in current) / len(current), 3)})
            current = [r] if (r is not None and flag(r)) else []
        return found

    mean_key = f"daq_aa_mean_{window}"
    summary = {
        "model_path": str(model), "map_path": str(density), "contour": contour, "daq_scores": str(daq_scores) if daq_scores else None,
        "residues": len(rows), "table": str(output_csv),
        "residues_mostly_outside_density": sum(r["fraction_above_contour"] < 0.5 for r in rows),
        "daq_unscored": sum(r["daq_aa"] is None for r in rows) if daq else None,
        "chain_breaks": [{"chain": r["chain"], "before_residue": r["residue_number"], "ca_gap_A": r["ca_gap_to_previous_A"]}
                         for r in rows if r["chain_break_before"]][:200],
        "runs_outside_density": [x for x in runs(lambda r: r["fraction_above_contour"] < 0.5) if x["length"] >= 3],
        f"runs_with_{mean_key}_below_minus_0.5": runs(lambda r: r.get(mean_key) is not None and r[mean_key] < -0.5) if daq else None,
        "note": ("Descriptive evidence only. A residue outside the density at this contour may be disordered, "
                 "misplaced, or simply weak; inspect overlays before deciding what to rebuild, refine or remove."),
    }
    output_csv.with_suffix(".json").write_text(json.dumps(summary, indent=2))
    return summary


# ------------------------------------------------------------------ model editing
def _parse_range(text):
    """'12-40' -> (12, 40); '7' -> (7, 7); '' -> None (whole chain)."""
    text = str(text).strip()
    if not text:
        return None
    m = re.fullmatch(r"(-?\d+)\s*(?:-\s*(-?\d+))?", text)
    if not m:
        raise ValueError(f"Residue range must look like '12-40' or '7', not {text!r}.")
    a = int(m.group(1))
    return a, int(m.group(2)) if m.group(2) else a


def edit_model(model: Path, output: Path, operations: list[dict]) -> dict:
    """Apply simple, explicit edits to one model and write a new file.

    Operations (applied in order):
      {"op": "delete", "chain": "A0", "residues": "1-44"}   delete a residue range (omit residues for the whole chain)
      {"op": "split", "chain": "A0", "after": 494, "new_chain": "A1"}   move residues after `after` to a new chain
      {"op": "rename_chain", "chain": "A1", "to": "B9"}
      {"op": "move_residues", "chain": "A1", "residues": "495-647", "to_chain": "B1"}   append a segment to another chain
    """
    import gemmi

    model, output = Path(model).resolve(), Path(output).resolve()
    if output == model:
        raise ValueError("Write edits to a new file.")
    structure = gemmi.read_structure(str(model))
    if len(structure) != 1:
        raise ValueError("Edit one candidate model at a time.")
    structure.setup_entities()
    m = structure[0]
    log = []

    def chain(name):
        for ch in m:
            if ch.name == name:
                return ch
        raise ValueError(f"Chain {name!r} not found.")

    def in_range(res, rng):
        return rng is None or rng[0] <= res.seqid.num <= rng[1]

    for op in operations:
        kind = op.get("op")
        if kind == "delete":
            ch, rng = chain(op["chain"]), _parse_range(op.get("residues", ""))
            before = len(ch)
            for i in range(len(ch) - 1, -1, -1):
                if in_range(ch[i], rng):
                    del ch[i]
            log.append({**op, "removed": before - len(ch)})
        elif kind == "split":
            ch, after, new = chain(op["chain"]), int(op["after"]), op["new_chain"]
            if any(c.name == new for c in m):
                raise ValueError(f"Chain {new!r} already exists.")
            moved = gemmi.Chain(new)
            for res in ch:
                if res.seqid.num > after:
                    moved.add_residue(res)
            for i in range(len(ch) - 1, -1, -1):
                if ch[i].seqid.num > after:
                    del ch[i]
            m.add_chain(moved)
            log.append({**op, "moved": len(moved)})
        elif kind == "rename_chain":
            if any(c.name == op["to"] for c in m):
                raise ValueError(f"Chain {op['to']!r} already exists.")
            chain(op["chain"]).name = op["to"]
            log.append(op)
        elif kind == "move_residues":
            src, dst, rng = chain(op["chain"]), chain(op["to_chain"]), _parse_range(op.get("residues", ""))
            taken = [res.clone() for res in src if in_range(res, rng)]
            existing = {r.seqid.num for r in dst}
            clash = [r.seqid.num for r in taken if r.seqid.num in existing]
            if clash:
                raise ValueError(f"Residue numbers {clash[:5]} already exist in chain {dst.name}.")
            for res in taken:
                dst.add_residue(res)
            for i in range(len(src) - 1, -1, -1):
                if in_range(src[i], rng):
                    del src[i]
            log.append({**op, "moved": len(taken)})
        else:
            raise ValueError(f"Unknown operation {kind!r}; use delete, split, rename_chain or move_residues.")
    structure.remove_empty_chains()
    for ch in m:   # keep residues in sequence order after moves
        residues = sorted((r.clone() for r in ch), key=lambda r: (r.seqid.num, r.seqid.icode))
        for i in range(len(ch) - 1, -1, -1):
            del ch[i]
        for r in residues:
            ch.add_residue(r)
    structure.setup_entities()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".pdb":
        structure.write_pdb(str(output))
    else:
        structure.make_mmcif_document().write_file(str(output))
    report = {"model_path": str(model), "edited_model": str(output), "operations": log,
              "chains": {ch.name: len(ch) for ch in m}, "residues": sum(len(ch) for ch in m)}
    output.with_suffix(".edits.json").write_text(json.dumps(report, indent=2))
    return report


# ------------------------------------------------------------------ executables
def _find(name: str, env_dir: str, fallback_dir: str) -> str:
    configured = os.environ.get(env_dir)
    for directory in ([configured] if configured else []) + [None, fallback_dir]:
        path = shutil.which(name) if directory is None else str(Path(directory) / name)
        if path and Path(path).exists() and os.access(path, os.X_OK):
            return path
    raise RuntimeError(f"{name} not found; set {env_dir} to its bin directory.")


def phenix_executable(tool: str) -> str:
    return _find(f"phenix.{tool}", "PHENIX_BIN", ZEBRA_PHENIX_BIN)


def rosetta_scripts_executable() -> str:
    for name in ("rosetta_scripts.static.linuxgccrelease", "rosetta_scripts.default.linuxgccrelease",
                 "rosetta_scripts.linuxgccrelease"):
        try:
            return _find(name, "ROSETTA_BIN", ZEBRA_ROSETTA_BIN)
        except RuntimeError:
            continue
    raise RuntimeError("rosetta_scripts not found; set ROSETTA_BIN to Rosetta's source/bin directory.")


def rosetta_database() -> str:
    path = os.environ.get("ROSETTA3_DB") or ZEBRA_ROSETTA_DB
    if not Path(path).is_dir():
        raise RuntimeError("Rosetta database not found; set ROSETTA3_DB.")
    return path


def _run(command: list[str], cwd: Path, log: Path, timeout: int, env=None) -> subprocess.CompletedProcess:
    (cwd / f"{log.stem}.command.txt").write_text(" ".join(command) + "\n")
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, start_new_session=True, env=env, check=False)
    log.write_text(completed.stdout + "\n--- stderr ---\n" + completed.stderr)
    return completed


def _tail(text: str, n: int = 1500) -> str:
    return text[-n:]


# ------------------------------------------------------------------ Phenix
def parse_map_model_cc(text: str) -> dict:
    out = {}
    for key in ("CC_mask", "CC_box", "CC_volume", "CC_peaks"):
        m = re.search(rf"{key}\s*:\s*(-?\d+\.\d+)", text)
        if m:
            out[key] = float(m.group(1))
    return out


def parse_molprobity(text: str) -> dict:
    patterns = {
        "molprobity_score": r"MolProbity score\s*=\s*(-?\d+\.?\d*)",
        "clashscore": r"Clashscore\s*=\s*(-?\d+\.?\d*)",
        "ramachandran_outliers_pct": r"Ramachandran outliers\s*=\s*(-?\d+\.?\d*)\s*%",
        "ramachandran_favored_pct": r"favored\s*=\s*(-?\d+\.?\d*)\s*%",
        "rotamer_outliers_pct": r"Rotamer outliers\s*=\s*(-?\d+\.?\d*)\s*%",
        "cbeta_deviations": r"C-beta deviations\s*=\s*(-?\d+)",
    }
    out = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            out[key] = float(m.group(1))
    return out


def validate_phenix(model: Path, density: Path, resolution: float, destination: Path) -> dict:
    model, density, destination = Path(model).resolve(), Path(density).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    try:  # Phenix 2.x replaced map_model_cc with map_correlations (same CC_mask/CC_box output)
        cc_tool = phenix_executable("map_correlations")
    except RuntimeError:
        cc_tool = phenix_executable("map_model_cc")
    cc = _run([cc_tool, str(model), str(density), f"resolution={resolution}"],
              destination, destination / "map_correlations.log", PHENIX_TIMEOUT)
    mp = _run([phenix_executable("molprobity"), str(model), "output.quiet=True"],
              destination, destination / "molprobity.log", PHENIX_TIMEOUT)
    metrics = {"model_path": str(model), "map_path": str(density), "resolution": resolution,
               **parse_map_model_cc(cc.stdout), **parse_molprobity(mp.stdout)}
    errors = []
    if cc.returncode or "CC_mask" not in metrics:
        errors.append(f"{Path(cc_tool).name} failed: " + _tail(cc.stdout + cc.stderr))
    if mp.returncode or "clashscore" not in metrics:
        errors.append("molprobity failed: " + _tail(mp.stdout + mp.stderr))
    if errors:
        metrics["errors"] = errors
    (destination / "validation.json").write_text(json.dumps(metrics, indent=2))
    return metrics


def refine_phenix(model: Path, density: Path, resolution: float, destination: Path, run: str = "",
                  macro_cycles: int = 5, nproc: int = 4, extra_parameters=(), label: str = "rsr") -> dict:
    """phenix.real_space_refine with the caller's choice of run steps and parameters.

    `run` is a '+'-joined subset of PHENIX_RUN_OPTIONS (empty keeps Phenix's defaults);
    `extra_parameters` are further PHIL assignments passed through unchanged (key=value).
    """
    model, density, destination = Path(model).resolve(), Path(density).resolve(), Path(destination).resolve()
    steps = [x for x in run.replace(",", "+").split("+") if x.strip()]
    unknown = [x for x in steps if x.strip() not in PHENIX_RUN_OPTIONS]
    if unknown:
        raise ValueError(f"Unknown run steps {unknown}; choose from {list(PHENIX_RUN_OPTIONS)}.")
    params = [str(p).strip() for p in extra_parameters if str(p).strip()]
    bad = [p for p in params if "=" not in p or p.startswith("-")]
    if bad:
        raise ValueError(f"Extra parameters must be PHIL key=value assignments: {bad}")
    destination.mkdir(parents=True, exist_ok=True)
    prefix = f"{model.stem}_{re.sub(r'[^A-Za-z0-9_.-]', '_', label)}"
    command = [phenix_executable("real_space_refine"), str(model), str(density), f"resolution={resolution}",
               f"macro_cycles={macro_cycles}", f"nproc={nproc}", f"output.prefix={prefix}", "output.overwrite=True"]
    if steps:
        command.append("run=" + "+".join(x.strip() for x in steps))
    command += params
    completed = _run(command, destination, destination / "real_space_refine.log", PHENIX_TIMEOUT)
    refined = sorted(destination.glob(f"{prefix}*real_space_refined*.cif")) or \
        sorted(destination.glob(f"{prefix}*real_space_refined*.pdb"))
    if completed.returncode or not refined:
        raise RuntimeError("phenix.real_space_refine failed: " + _tail(completed.stdout + completed.stderr))
    return {"input_model": str(model), "map_path": str(density), "run": "+".join(steps) or "phenix defaults",
            "macro_cycles": macro_cycles, "extra_parameters": params, "refined_model": str(refined[-1]),
            "command": " ".join(command), "log": str(destination / "real_space_refine.log")}


# ------------------------------------------------------------------ Rosetta
ROSETTA_XML = """<ROSETTASCRIPTS>
  <SCOREFXNS>
    <ScoreFunction name="dens" weights="{weights}">
      <Reweight scoretype="elec_dens_fast" weight="{density_weight}"/>
    </ScoreFunction>
  </SCOREFXNS>
  <MOVERS>
    <SetupForDensityScoring name="setup_density"/>
    <LoadDensityMap name="load_density" mapfile="{mapfile}"/>
    <FastRelax name="relax" scorefxn="dens" repeats="{repeats}" cartesian="{cartesian}"/>
  </MOVERS>
  <PROTOCOLS>
    <Add mover="setup_density"/>
    <Add mover="load_density"/>
    <Add mover="relax"/>
  </PROTOCOLS>
  <OUTPUT scorefxn="dens"/>
</ROSETTASCRIPTS>
"""


def _to_single_letter_chains(model: Path, pdb_out: Path) -> dict:
    import gemmi

    structure = gemmi.read_structure(str(model))
    structure.remove_hydrogens()
    names = [ch.name for ch in structure[0]]
    if len(set(names)) > len(CHAIN_LETTERS):
        raise ValueError("Too many chains for PDB-format chain identifiers.")
    mapping = {}
    for chain, letter in zip(structure[0], CHAIN_LETTERS):
        mapping[letter] = chain.name
        chain.name = letter
    structure.write_pdb(str(pdb_out))
    return mapping


def _restore_chains(pdb_in: Path, out: Path, mapping: dict) -> None:
    import gemmi

    structure = gemmi.read_structure(str(pdb_in))
    structure.remove_hydrogens()
    for chain in structure[0]:
        chain.name = mapping.get(chain.name, chain.name)
    structure.setup_entities()
    structure.make_mmcif_document().write_file(str(out))


def refine_rosetta(model: Path, density: Path, resolution: float, destination: Path,
                   repeats: int = 1, nstruct: int = 1, density_weight: float = 35.0, cartesian: bool = True) -> dict:
    model, density, destination = Path(model).resolve(), Path(density).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    pdb_in = destination / f"{model.stem}_rosetta_input.pdb"
    mapping = _to_single_letter_chains(model, pdb_in)
    (destination / "chain_mapping.json").write_text(json.dumps(mapping, indent=2))
    xml = destination / "density_relax.xml"
    xml.write_text(ROSETTA_XML.format(density_weight=density_weight, mapfile=str(density), repeats=repeats,
                                      weights="ref2015_cart" if cartesian else "ref2015", cartesian=int(bool(cartesian))))
    command = [rosetta_scripts_executable(), "-database", rosetta_database(), "-parser:protocol", str(xml),
               "-in:file:s", str(pdb_in), "-edensity:mapreso", str(resolution), "-edensity:cryoem_scatterers",
               "-default_max_cycles", "200", "-ignore_unrecognized_res", "-nstruct", str(nstruct),
               "-out:path:all", str(destination), "-out:suffix", "_relaxed", "-overwrite"]
    completed = _run(command, destination, destination / "rosetta_relax.log", ROSETTA_TIMEOUT)
    outputs = sorted(destination.glob(f"{pdb_in.stem}_relaxed_*.pdb"))
    if completed.returncode or not outputs:
        raise RuntimeError("Rosetta density relax failed: " + _tail(completed.stdout + completed.stderr))
    scores = {}
    score_file = destination / "score_relaxed.sc"
    if score_file.exists():
        lines = [l.split() for l in score_file.read_text().splitlines() if l.startswith("SCORE:")]
        if len(lines) > 1:
            header = lines[0][1:]
            for row in lines[1:]:
                values = dict(zip(header, row[1:]))
                scores[values.get("description", "")] = {k: values[k] for k in ("total_score", "elec_dens_fast") if k in values}
    best = min(outputs, key=lambda p: float(scores.get(p.stem, {}).get("total_score", 0.0)))
    final = destination / f"{model.stem}_rosetta_relaxed.cif"
    _restore_chains(best, final, mapping)
    return {"input_model": str(model), "map_path": str(density), "repeats": repeats, "nstruct": nstruct,
            "density_weight": density_weight, "cartesian": bool(cartesian), "refined_model": str(final), "rosetta_output": str(best),
            "scores": scores, "log": str(destination / "rosetta_relax.log")}
