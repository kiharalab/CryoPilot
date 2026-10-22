from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gemmi

import refinement

FIELDS = ["candidate_index", "model_number", "index", "chain", "residue_number", "insertion_code", "residue_name",
          "scored", "aa", "ca", "ss", "total", "aa_smoothed", "ca_smoothed", "ss_smoothed", "total_smoothed"]


def chain_model(path: Path, chains: dict[str, int]) -> None:
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    for name, length in chains.items():
        chain = gemmi.Chain(name)
        for i in range(1, length + 1):
            residue = gemmi.Residue()
            residue.name, residue.seqid = "ALA", gemmi.SeqId(i, " ")
            atom = gemmi.Atom()
            atom.name, atom.element, atom.pos = "CA", gemmi.Element("C"), gemmi.Position(3.8 * i, 0, 0)
            residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    structure.make_mmcif_document().write_file(str(path))


def write_scores(path: Path, chains: dict[str, list]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        index = 0
        for chain, values in chains.items():
            for number, value in enumerate(values, 1):
                index += 1
                scored = value is not None
                writer.writerow({"candidate_index": 1, "model_number": 1, "index": index, "chain": chain,
                                 "residue_number": number, "insertion_code": "", "residue_name": "ALA",
                                 "scored": str(scored), "aa": "" if not scored else value})


def write_map(path: Path, shape=(40, 40, 120), filled_x=(0.0, 80.0), value=1.0) -> None:
    """Map with 1 Å voxels, density `value` for x in `filled_x` (Å) and 0 elsewhere."""
    import mrcfile
    import numpy as np

    data = np.zeros(shape, dtype=np.float32)            # z, y, x
    lo, hi = int(filled_x[0]), int(filled_x[1])
    data[:, :, lo:hi] = value
    with mrcfile.new(path, overwrite=True) as mrc:
        mrc.set_data(data)
        mrc.voxel_size = 1.0


class ResidueSupportTests(unittest.TestCase):
    def test_reports_density_daq_and_chain_breaks_without_changing_the_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chain_model(root / "model.cif", {"A0": 30})       # Cα at x = 3.8 * i Å, y = z = 0
            write_map(root / "map.mrc", filled_x=(0.0, 82.0))  # residues 1..21 (x <= 79.8 Å) inside, 22..30 outside
            a = [1.0] * 20 + [None] * 10                       # DAQ leaves the outside residues unscored
            write_scores(root / "scores.csv", {"A0": a})
            before = (root / "model.cif").read_bytes()
            report = refinement.residue_support(root / "model.cif", root / "map.mrc", 0.5, root / "support.csv",
                                                root / "scores.csv")
            self.assertEqual((root / "model.cif").read_bytes(), before)
            self.assertEqual(report["residues"], 30)
            self.assertEqual(report["daq_unscored"], 10)
            self.assertEqual(report["runs_outside_density"], [
                {"chain": "A0", "start": 22, "end": 30, "length": 9, "mean_fraction_above_contour": 0.0}])
            self.assertEqual(report["chain_breaks"], [])
            with (root / "support.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["fraction_above_contour"], "1.0")
            self.assertEqual(rows[0]["daq_aa_mean_19"], "1.0")


class EditModelTests(unittest.TestCase):
    def test_delete_split_move_and_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chain_model(root / "model.cif", {"A0": 20, "B1": 10})
            ops = [{"op": "delete", "chain": "A0", "residues": "1-3"},
                   {"op": "split", "chain": "A0", "after": 15, "new_chain": "X"},
                   {"op": "move_residues", "chain": "X", "residues": "16-20", "to_chain": "B1"},
                   {"op": "rename_chain", "chain": "B1", "to": "B2"}]
            report = refinement.edit_model(root / "model.cif", root / "edited.cif", ops)
            self.assertEqual(report["chains"], {"A0": 12, "B2": 15})
            kept = gemmi.read_structure(str(root / "edited.cif"))
            numbers = {ch.name: [r.seqid.num for r in ch] for ch in kept[0]}
            self.assertEqual(numbers["A0"], list(range(4, 16)))
            self.assertEqual(numbers["B2"], list(range(1, 11)) + list(range(16, 21)))
            self.assertTrue((root / "edited.edits.json").exists())

    def test_rejects_overwrite_and_unknown_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chain_model(root / "model.cif", {"A": 5})
            with self.assertRaisesRegex(ValueError, "new file"):
                refinement.edit_model(root / "model.cif", root / "model.cif", [])
            with self.assertRaisesRegex(ValueError, "Unknown operation"):
                refinement.edit_model(root / "model.cif", root / "out.cif", [{"op": "mirror"}])


class PhenixTests(unittest.TestCase):
    def test_parsers_read_cc_and_molprobity(self):
        cc = "  CC_mask  : 0.7914\n  CC_volume: 0.7703\n  CC_peaks : 0.6512\n  CC_box   : 0.8101\n"
        mp = ("  Ramachandran outliers =   0.25 %\n                favored =  96.80 %\n"
              "  Rotamer outliers      =   1.10 %\n  C-beta deviations     =     2\n"
              "  Clashscore            =   4.31\n  MolProbity score      =   1.62\n")
        self.assertEqual(refinement.parse_map_model_cc(cc)["CC_mask"], 0.7914)
        metrics = refinement.parse_molprobity(mp)
        self.assertEqual(metrics["clashscore"], 4.31)
        self.assertEqual(metrics["ramachandran_favored_pct"], 96.8)
        self.assertEqual(metrics["cbeta_deviations"], 2)

    def test_real_space_refine_passes_the_agents_choices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "m.cif").write_text("x"); (root / "map.mrc").write_text("x")
            out = root / "rsr"
            def fake_run(command, **kwargs):
                self.assertIn("run=minimization_global+morphing+adp", command)
                self.assertIn("resolution=3.1", command)
                self.assertIn("secondary_structure.enabled=True", command)
                (out / "m_ss_real_space_refined_000.cif").write_text("refined")
                return SimpleNamespace(returncode=0, stdout="done", stderr="")
            with patch.object(refinement, "phenix_executable", return_value="phenix.real_space_refine"), \
                 patch.object(refinement.subprocess, "run", side_effect=fake_run):
                result = refinement.refine_phenix(root / "m.cif", root / "map.mrc", 3.1, out,
                                                  run="minimization_global+morphing+adp",
                                                  extra_parameters=["secondary_structure.enabled=True"], label="ss")
            self.assertTrue(result["refined_model"].endswith("real_space_refined_000.cif"))
            self.assertTrue((out / "real_space_refine.command.txt").exists())

    def test_default_run_uses_phenix_defaults_and_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "rsr"
            def fake_run(command, **kwargs):
                self.assertFalse(any(c.startswith("run=") for c in command))
                (out / "m_rsr_real_space_refined_000.cif").write_text("refined")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(refinement, "phenix_executable", return_value="phenix.real_space_refine"), \
                 patch.object(refinement.subprocess, "run", side_effect=fake_run):
                refinement.refine_phenix(root / "m.cif", root / "map.mrc", 3.0, out)
                with self.assertRaisesRegex(ValueError, "Unknown run steps"):
                    refinement.refine_phenix(root / "m.cif", root / "map.mrc", 3.0, out, run="minimization_global+magic")
                with self.assertRaisesRegex(ValueError, "key=value"):
                    refinement.refine_phenix(root / "m.cif", root / "map.mrc", 3.0, out, extra_parameters=["--overwrite"])

    def test_failed_refinement_raises_with_log_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(refinement, "phenix_executable", return_value="phenix.real_space_refine"), \
                 patch.object(refinement.subprocess, "run",
                              return_value=SimpleNamespace(returncode=1, stdout="", stderr="Sorry: bad map")):
                with self.assertRaisesRegex(RuntimeError, "bad map"):
                    refinement.refine_phenix(root / "m.cif", root / "map.mrc", 3.0, root / "rsr")


class RosettaTests(unittest.TestCase):
    def test_density_relax_round_trips_multi_letter_chain_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chain_model(root / "model.cif", {"A0": 3, "B1": 2})
            (root / "map.mrc").write_text("x")
            out = root / "relax"
            def fake_run(command, **kwargs):
                xml = Path(command[command.index("-parser:protocol") + 1]).read_text()
                self.assertIn('scoretype="elec_dens_fast" weight="35.0"', xml)
                self.assertIn("LoadDensityMap", xml)
                self.assertIn("-edensity:cryoem_scatterers", command)
                source = Path(command[command.index("-in:file:s") + 1])
                (out / f"{source.stem}_relaxed_0001.pdb").write_text(source.read_text())
                (out / "score_relaxed.sc").write_text(
                    "SEQUENCE:\nSCORE: total_score elec_dens_fast description\n"
                    f"SCORE: -120.0 -50.0 {source.stem}_relaxed_0001\n")
                return SimpleNamespace(returncode=0, stdout="ok", stderr="")
            with patch.object(refinement, "rosetta_scripts_executable", return_value="rosetta_scripts"), \
                 patch.object(refinement, "rosetta_database", return_value="/db"), \
                 patch.object(refinement.subprocess, "run", side_effect=fake_run):
                result = refinement.refine_rosetta(root / "model.cif", root / "map.mrc", 3.0, out)
                torsion = refinement.refine_rosetta(root / "model.cif", root / "map.mrc", 3.0, out, cartesian=False)
            self.assertFalse(torsion["cartesian"])
            self.assertIn('weights="ref2015"', (out / "density_relax.xml").read_text())
            self.assertEqual(json.loads((out / "chain_mapping.json").read_text()), {"A": "A0", "B": "B1"})
            final = gemmi.read_structure(result["refined_model"])
            self.assertEqual([ch.name for ch in final[0]], ["A0", "B1"])
            self.assertEqual(result["scores"]["model_rosetta_input_relaxed_0001"]["total_score"], "-120.0")


if __name__ == "__main__":
    unittest.main()
