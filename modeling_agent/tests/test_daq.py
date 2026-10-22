from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gemmi
import mrcfile
import numpy as np

from daq import daq_compute as daq
from map_utils import read_mrc, resample_to_1a, write_mrc, flip_map_copy


def make_structure():
    structure = gemmi.Structure()
    structure.name = "test"
    for number, positions in ((11, (0, 5)), (22, (10, 15))):
        model = gemmi.Model(number)
        chain = gemmi.Chain("A0")
        for index, x in enumerate(positions):
            residue = gemmi.Residue()
            residue.name = "ALA"
            residue.seqid = gemmi.SeqId(10, "A" if index else " ")
            atom = gemmi.Atom()
            atom.name = "CA"
            atom.element = gemmi.Element("C")
            atom.pos = gemmi.Position(x, 0, 0)
            residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
        structure.add_model(model)
    return structure


class MapTests(unittest.TestCase):
    def test_invalid_map_metadata_and_density_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.mrc"
            write_mrc(path, np.ones((2, 2, 2)), (0, 0, 0), (1, 1, 1))
            with mrcfile.open(path, mode="r+") as mrc:
                mrc.header.mapc = 2
            with self.assertRaisesRegex(ValueError, "axis permutation"):
                read_mrc(path)
            with self.assertWarnsRegex(RuntimeWarning, "NaN"):
                write_mrc(path, np.full((2, 2, 2), np.nan), (0, 0, 0), (1, 1, 1))
            with self.assertRaisesRegex(ValueError, "density must be finite"):
                read_mrc(path)
            write_mrc(path, np.ones((2, 2, 2)), (0, 0, 0), (0, 1, 1))
            with self.assertRaisesRegex(ValueError, "spacing"):
                read_mrc(path)

    def test_resampling_keeps_physical_coordinates(self):
        data = np.indices((3, 3, 3))[2].astype(np.float32) * 2
        result, origin, step = resample_to_1a(data, (10, 20, 30), (2, 2, 2))
        self.assertEqual(result.shape, (5, 5, 5))
        np.testing.assert_allclose(result[0, 0], [0, 1, 2, 3, 4])
        self.assertEqual(origin, (10, 20, 30))
        self.assertEqual(step, (1, 1, 1))

    def test_axis_permutation_and_starts(self):
        standard = np.arange(60, dtype=np.float32).reshape(3, 4, 5)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "permuted.mrc"
            with mrcfile.new(path) as mrc:
                mrc.set_data(standard.transpose(2, 0, 1))  # stored X,Z,Y
                mrc.header.mapc, mrc.header.mapr, mrc.header.maps = 2, 3, 1
                mrc.header.mx, mrc.header.my, mrc.header.mz = 5, 4, 3
                mrc.voxel_size = (1, 2, 3)
                mrc.header.nxstart, mrc.header.nystart, mrc.header.nzstart = 4, 3, 2
            data, origin, step = read_mrc(path)
            np.testing.assert_array_equal(data, standard)
            self.assertEqual(origin, (2, 8, 9))
            self.assertEqual(step, (1, 2, 3))

    def test_flip_preserves_original_and_records_reflection(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary) / "map.mrc", Path(temporary) / "flipped.mrc"
            data = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
            write_mrc(source, data, (10, 20, 30), (1, 2, 3))
            before = hashlib.sha256(source.read_bytes()).digest()
            result = flip_map_copy(source, target, "z")
            flipped, origin, step = read_mrc(target)
            np.testing.assert_array_equal(flipped, data[::-1])
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)
            matrix = np.array(result["reflection_matrix"])
            np.testing.assert_allclose(matrix @ [10, 20, 30, 1], [10, 20, 33, 1])
            self.assertAlmostEqual(np.linalg.det(matrix[:3, :3]), -1)
            with self.assertRaises(ValueError):
                flip_map_copy(source, source)

    def test_grid_inference_receives_1a_patches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "map.mrc"
            write_mrc(path, np.ones((3, 3, 3)), (1, 2, 3), (2, 2, 2))
            args = SimpleNamespace(map=path, contour=0.2, stride=1, max_points=10,
                                   model_path=None, backend="cpu", gpu_id=0, batch_size=1, seed=0)
            class Compute:
                normalize_volume = staticmethod(lambda data: data)
                def extract_threshold_points(self, data, origin, step, **kwargs):
                    self.shape, self.step = data.shape, step
                    return np.array([[1, 2, 3]], dtype=np.float32), np.ones(1)
                def extract_patches(self, data, points, origin, step, patch_size):
                    return np.zeros((1, 1, patch_size, patch_size, patch_size))
                def compute_log_ratio_scores(self, *args):
                    return np.zeros((1, 32))
            compute = Compute()
            model = SimpleNamespace(predict_batched=lambda *a, **kw: (np.zeros((1, 20)), np.zeros((1, 6)), np.zeros((1, 3))))
            onnx = SimpleNamespace(load_model=lambda *a, **kw: model)
            with patch.object(daq, "_load_daq_module", side_effect=[compute, onnx]):
                daq._compute_grid_from_map(args, root / "grid.npy")
            self.assertEqual(compute.shape, (5, 5, 5))
            self.assertEqual(compute.step, (1, 1, 1))
            metadata = json.loads((root / "grid.json").read_text())
            self.assertEqual(metadata["input_spacing_A"], [2, 2, 2])


class SamplingTests(unittest.TestCase):
    def test_every_grid_point_above_half_contour_is_scored(self):
        compute = daq._load_daq_module("compute")
        data = np.zeros((6, 6, 6), dtype=np.float32)
        data[1:5, 1:5, 1:5] = 1.0
        points, _ = compute.extract_threshold_points(data, (0, 0, 0), (1, 1, 1), contour=0.5, stride=1, max_points=None)
        self.assertEqual(len(points), 64)

    def test_agent_tool_cannot_lower_sampling(self):
        import tools
        properties = tools.run_daq.params_json_schema["properties"]
        for name in ("stride", "max_points", "seed"):
            self.assertNotIn(name, properties)
        source = Path(tools.__file__).read_text()
        self.assertIn('"--stride",\n        "1",\n        "--max-points",\n        "0",', source)


class ResidueScoreTests(unittest.TestCase):
    def test_smoothing_never_fills_unscored_residue_or_crosses_chains(self):
        result = daq._window_average(np.array(["A", "A", "B"]), np.array([10, 11, 10]),
                                     np.array([1.0, np.nan, 8.0]), 9)
        np.testing.assert_allclose(result, [1, np.nan, 8], equal_nan=True)

    def test_missing_ca_is_not_replaced_by_centroid(self):
        atom = gemmi.Atom()
        atom.name, atom.pos = "N", gemmi.Position(1, 2, 3)
        self.assertTrue(np.isnan(daq._residue_coords([[atom]], "CA")).all())

    def test_multimodel_private_scoring_rejected(self):
        with self.assertRaisesRegex(ValueError, "one candidate"):
            daq._protein_residues(make_structure())

    def test_cli_candidate_scores_are_independent_and_coverage_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "model.cif"
            make_structure().make_mmcif_document().write_file(str(source))
            before = source.read_bytes()
            grid = np.zeros((3, 32), dtype=np.float32)
            grid[:, 0] = [0, 5, 10]
            grid[:, 3:] = np.array([1, 2, 5])[:, None]
            np.save(root / "grid.npy", grid)
            command = [sys.executable, str(daq.__file__), "--model", str(source), "--grid", str(root / "grid.npy"),
                       "--output-dir", str(root / "scores"), "--half-window", "9"]
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout.splitlines()[-1])
            self.assertEqual(len(result["candidates"]), 2)
            first, second = result["candidates"]
            self.assertAlmostEqual(first["summary"]["aa"]["mean"], 1.5)
            self.assertAlmostEqual(second["summary"]["aa"]["mean"], 5)
            self.assertEqual(second["coverage"], {"scored": 1, "total": 2})
            with Path(second["scoreTable"]).open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[1]["insertion_code"], "A")
            self.assertEqual(rows[1]["model_number"], "22")
            self.assertEqual(rows[1]["candidate_index"], "2")
            self.assertEqual(rows[1]["aa"], "")
            self.assertEqual(rows[1]["aa_smoothed"], "")
            annotated = gemmi.read_structure(second["scoredCifs"]["aa"])
            self.assertEqual(annotated[0][0][1][0].occ, 0)
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(Path(first["scorePlot"]).exists())
            selected = subprocess.run(command + ["--candidate-index", "2"], capture_output=True, text=True)
            self.assertEqual(selected.returncode, 0, selected.stdout)
            self.assertEqual(len(json.loads(selected.stdout.splitlines()[-1])["candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
