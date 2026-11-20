from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gemmi
import numpy as np

import tools
import vesper_align
from map_utils import read_mrc, write_mrc, flip_map_copy
from test_daq import make_structure


FIT = "#0 R={0 0 90} MTX={0 -1 0 1 0 0 0 0 1} T={10 -3 4} sco= 100 zsco= 2\n"


class VesperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.model = self.root / "models.cif"
        make_structure().make_mmcif_document().write_file(str(self.model))
        write_mrc(self.root / "map.mrc", np.ones((3, 3, 3)), (10, 20, 30), (2, 2, 2))

    def simulate(self, model, output, resolution):
        selected = gemmi.read_structure(str(model))
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].num, 22)
        write_mrc(output, np.ones((3, 3, 3)), (0, 0, 0), (1, 1, 1))

    def fit(self, **kwargs):
        params = dict(resolution=3, contour=.1, candidate_index=2, grid_spacing=2,
                      angular_step=30, top_n=2, bandwidth=4, cores=2,
                      simulate=self.simulate, environment={})
        params.update(kwargs)
        return vesper_align.align(self.model, self.root / "map.mrc", self.root / "fit", **params)

    def test_transform_applies_in_physical_coordinates_and_preserves_source(self):
        before = self.model.read_bytes()
        def fake_run(command, **kwargs):
            self.assertEqual(command[0], "/test/VESPER")
            self.assertEqual(command[command.index("-s") + 1], "2")
            from map_utils import read_mrc
            data, origin, step = read_mrc(kwargs["cwd"] / "map_unified.mrc")
            self.assertEqual(data.shape, (5, 5, 5))
            self.assertEqual(origin, (10, 20, 30))
            self.assertEqual(step, (1, 1, 1))
            return SimpleNamespace(returncode=0, stdout=FIT, stderr="")
        with patch.object(vesper_align, "executable", return_value="/test/VESPER"), \
             patch.object(vesper_align.subprocess, "run", side_effect=fake_run):
            result = self.fit()
        fitted = gemmi.read_structure(result["aligned_model"])
        xyz = [[a.pos.x, a.pos.y, a.pos.z] for c in fitted[0] for r in c for a in r]
        np.testing.assert_allclose(xyz, [[10, 7, 4], [10, 12, 4]])
        self.assertFalse(result["model_handedness_changed"])
        self.assertEqual(self.model.read_bytes(), before)
        self.assertTrue(Path(result["result_path"]).exists())

    def test_stale_result_not_accepted_after_empty_success(self):
        (self.root / "fit").mkdir()
        (self.root / "fit/fit_result.json").write_text('{"ok": true}')
        with patch.object(vesper_align, "executable", return_value="/test/VESPER"), \
             patch.object(vesper_align.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="failed", stderr="")):
            with self.assertRaisesRegex(RuntimeError, "no valid fitted transforms"):
                self.fit()

    def test_reflection_and_nonrigid_transform_rejected(self):
        for matrix in ("-1 0 0 0 1 0 0 0 1", "2 0 0 0 1 0 0 0 1"):
            with self.assertRaisesRegex(ValueError, "chirality"):
                vesper_align.parse_fits(FIT.replace("0 -1 0 1 0 0 0 0 1", matrix))

    def test_invalid_candidate_and_parameters(self):
        with self.assertRaisesRegex(ValueError, "candidate_index"):
            self.fit(candidate_index=3)
        with self.assertRaisesRegex(ValueError, "positive"):
            self.fit(resolution=-1)

    def test_no_overwrite_of_source_model(self):
        self.model = self.root / "fit/aligned_model.cif"
        self.model.parent.mkdir()
        make_structure().make_mmcif_document().write_file(str(self.model))
        before = self.model.read_bytes()
        with patch.object(vesper_align, "executable", return_value="/test/VESPER"), \
             patch.object(vesper_align.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=FIT, stderr="")):
            with self.assertRaisesRegex(ValueError, "overwrite"):
                self.fit()
        self.assertEqual(self.model.read_bytes(), before)

    @unittest.skipUnless(os.environ.get("CRYOPILOT_TEST_VESPER") == "1", "optional installed VESPER/ChimeraX smoke test")
    def test_installed_vesper_recovers_rotated_translated_model(self):
        structure = gemmi.Structure()
        model, chain = gemmi.Model(1), gemmi.Chain("A")
        points = np.array([[0, 0, 0], [5, 0, 0], [0, 8, 0], [1, 2, 11]], dtype=float)
        for i, point in enumerate(points):
            residue = gemmi.Residue()
            residue.name, residue.seqid = "ALA", gemmi.SeqId(i + 1, " ")
            atom = gemmi.Atom()
            atom.name, atom.element, atom.pos = "CA", gemmi.Element("C"), gemmi.Position(*point)
            residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
        structure.add_model(model)
        structure.make_mmcif_document().write_file(str(self.model))
        expected = vesper_align.parse_fits(FIT)[0]["rotation_translation_matrix"]
        target = vesper_align.transformed_structure(structure, expected)
        target.make_mmcif_document().write_file(str(self.root / "target.cif"))
        tools._simulate_model_map(self.root / "target.cif", self.root / "map.mrc", 3)
        # Keep every plausible placement inside the test volume, so the wrong
        # hand is scored in empty density instead of losing out-of-bounds atoms.
        density, origin, step = read_mrc(self.root / "map.mrc")
        write_mrc(self.root / "map.mrc", np.pad(density, 12),
                  tuple(o - 12 * s for o, s in zip(origin, step)), step)
        result = self.fit(candidate_index=1, simulate=tools._simulate_model_map,
                          grid_spacing=1, angular_step=90, top_n=3, bandwidth=2, contour=.01,
                          environment=tools._shell_environment())
        fitted = gemmi.read_structure(result["aligned_model"])
        actual = np.array([[a.pos.x, a.pos.y, a.pos.z] for c in fitted[0] for r in c for a in r])
        target_xyz = np.array([[a.pos.x, a.pos.y, a.pos.z] for c in target[0] for r in c for a in r])
        self.assertLess(np.sqrt(np.mean(np.sum((actual - target_xyz) ** 2, axis=1))), 1.5)
        # The model is chiral, so the mirrored map must give a worse best placement.
        flip_map_copy(self.root / "map.mrc", self.root / "flipped.mrc")
        other = vesper_align.align(self.model, self.root / "flipped.mrc", self.root / "fit_flipped",
                                   resolution=3, contour=.01, candidate_index=1, grid_spacing=1,
                                   angular_step=90, top_n=3, bandwidth=2, cores=2,
                                   simulate=tools._simulate_model_map, environment=tools._shell_environment())
        self.assertGreater(result["dot_score"], other["dot_score"])

if __name__ == "__main__":
    unittest.main()
