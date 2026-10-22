"""Headless map simulation and Q-score bridge. Alignment is performed by VESPER."""
import csv
import json
import sys
from pathlib import Path


def execute(session, config):
    import numpy as np
    from chimerax.core.commands import run

    quote = lambda value: json.dumps(str(value))
    structure = run(session, "open " + quote(config["model_path"]))[0]
    if config["operation"] == "simulate":
        volume = run(session, f"molmap #{structure.id_string} {config['resolution']} gridSpacing 1")
        run(session, f"save {quote(config['output_path'])} models #{volume.id_string}")
        return {"ok": True, "map_path": config["output_path"]}
    if config["operation"] != "qscore":
        raise ValueError("Unknown ChimeraX support operation.")
    try:
        from chimerax.qscore.qscore import q_score
    except ImportError as exc:
        raise RuntimeError("Install the ChimeraX QScore bundle: toolshed install QScore") from exc
    volume = run(session, "open " + quote(config["map_path"]))[0]
    _, (atoms, scores) = q_score(structure.residues, volume, logger=session.logger,
                                ref_sigma=0.6, points_per_shell=8, max_rad=2.0,
                                step=0.1, random_seed=1985)
    values = {atom: float(score) for atom, score in zip(atoms, scores) if np.isfinite(score)}
    with Path(config["output_path"]).open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["chain", "residue_number", "insertion_code", "residue_name", "atom", "qscore"])
        for atom in structure.atoms:
            if atom.element.name == "H":
                continue
            residue = atom.residue
            writer.writerow([residue.chain_id, residue.number, residue.insertion_code,
                             residue.name, atom.name, values.get(atom, "")])
    finite = list(values.values())
    return {"ok": True, "score_table": config["output_path"], "scored_atoms": len(finite),
            "mean_qscore": float(np.mean(finite)) if finite else None,
            "settings": {"implementation": "ChimeraX QScore", "ref_sigma": 0.6,
                         "points_per_shell": 8, "max_radius": 2.0, "step": 0.1, "seed": 1985}}


if "session" in globals():
    # The agent stages config.json beside this script to avoid argument splitting
    # by cluster launchers. Explicit arguments remain supported for manual use.
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("config.json")
    config = json.loads(config_path.read_text())
    try:
        result = execute(session, config)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    Path(config["result_path"]).write_text(json.dumps(result, allow_nan=False, indent=2))
    from chimerax.core.commands import run
    run(session, "exit")
