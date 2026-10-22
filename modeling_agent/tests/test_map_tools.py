from __future__ import annotations

import asyncio
import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gemmi
import numpy as np
from PIL import Image
from agents.tool_context import ToolContext

import tools
from map_utils import write_mrc
from test_daq import make_structure


class RegisteredMapToolTests(unittest.TestCase):
    def _overlay_fixture(self, root):
        structure = make_structure()
        del structure[1]
        structure.make_mmcif_document().write_file(str(root / "model.cif"))
        z, y, x = np.indices((24, 24, 24))
        density = np.exp(-((x - 8) ** 2 + (y - 8) ** 2 + (z - 8) ** 2) / 20)
        write_mrc(root / "map.mrc", density.astype(np.float32), (-8, -8, -8), (1, 1, 1))

    def test_overlay_renders_three_slab_images_without_opengl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._overlay_fixture(root)
            arguments = json.dumps({"model_path": "model.cif", "map_path": "map.mrc", "contour": .2,
                                    "output_dir": "views", "color_by_bfactor": True})
            context = ToolContext(context=None, tool_name="render_map_model", tool_call_id="test", tool_arguments=arguments)
            with patch.object(tools, "ROOT", root), patch.object(tools.subprocess, "run", side_effect=AssertionError("no subprocess")):
                result = asyncio.run(tools.render_map_model.on_invoke_tool(context, arguments))
            self.assertIsInstance(result, list, result)
            self.assertEqual(len(result), 4)
            metadata = json.loads(result[0].text)
            self.assertIn("red -1.5", metadata["DAQ_scale"])
            self.assertEqual(len(metadata["images"]), 3)
            for path in metadata["images"]:
                with Image.open(path) as image:
                    self.assertLess(np.array(image.convert("L")).mean(), 254.9)

    def test_overlay_rejects_empty_or_unsupported_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._overlay_fixture(root)
            for selection, message in (("chain ZZ", "Focus selection is empty"), ("name CA", "Unsupported selection")):
                arguments = json.dumps({"model_path": "model.cif", "map_path": "map.mrc", "contour": .2,
                                        "output_dir": "views", "selection": selection})
                context = ToolContext(context=None, tool_name="render_map_model", tool_call_id="test", tool_arguments=arguments)
                with patch.object(tools, "ROOT", root):
                    result = asyncio.run(tools.render_map_model.on_invoke_tool(context, arguments))
                self.assertIsInstance(result, str)
                self.assertIn(message, result)

    def test_contour_views_report_components_per_level(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._overlay_fixture(root)
            arguments = json.dumps({"map_path": "map.mrc", "contour": .3, "output_dir": "contour"})
            context = ToolContext(context=None, tool_name="render_map_views", tool_call_id="test", tool_arguments=arguments)
            with patch.object(tools, "ROOT", root), patch.object(tools.subprocess, "run", side_effect=AssertionError("no subprocess")):
                result = asyncio.run(tools.render_map_views.on_invoke_tool(context, arguments))
            self.assertIsInstance(result, list, result)
            metadata = json.loads(result[0].text)
            self.assertEqual([round(v, 3) for v in metadata["levels"]], [.24, .3, .36])
            self.assertEqual([s["components"] for s in metadata["component_stats"]], [1, 1, 1])
            self.assertTrue(Path(metadata["image"]).exists())

    def test_new_tools_registered_with_instructions(self):
        names = {tool.name for tool in tools.ALL_TOOLS}
        self.assertTrue({"flip_map", "align_model_to_map", "render_map_model", "render_map_views", "search_pdb_homologs",
                         "download_pdb_homolog", "run_qscore", "compare_map_hands"} <= names)

    @unittest.skipUnless(os.environ.get("CRYOAGENT_TEST_QSCORE") == "1", "optional installed QScore smoke test")
    def test_installed_qscore_scores_aligned_density_and_marks_missing_atoms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            structure = make_structure()
            del structure[1]
            # A third residue lies outside the map and must stay explicitly unscored.
            outside = structure[0][0][0].clone()
            outside.seqid = gemmi.SeqId(99, " ")
            outside[0].pos = gemmi.Position(100, 100, 100)
            structure[0][0].add_residue(outside)
            structure.make_mmcif_document().write_file(str(root / "model.cif"))
            z, y, x = np.indices((33, 33, 43)) * .5 - 8
            density = sum(np.exp(-((x - center) ** 2 + y ** 2 + z ** 2) / (2 * .6 ** 2)) for center in (0, 5))
            write_mrc(root / "map.mrc", density.astype(np.float32), (-8, -8, -8), (.5, .5, .5))
            arguments = json.dumps({"model_path": "model.cif", "map_path": "map.mrc", "output_dir": "q"})
            context = ToolContext(context=None, tool_name="run_qscore", tool_call_id="test", tool_arguments=arguments)
            with patch.object(tools, "ROOT", root):
                result = json.loads(asyncio.run(tools.run_qscore.on_invoke_tool(context, arguments)))
            self.assertEqual(result["scored_atoms"], 2)
            self.assertGreater(result["mean_qscore"], .95)
            import csv
            with Path(result["score_table"]).open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[2]["qscore"], "")

    def test_daq_tool_exposes_candidates_without_averaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "model.cif").write_text("fixture")
            (root / "map.mrc").touch()
            def fake_run(command, **kwargs):
                self.assertEqual(command[command.index("--half-window") + 1], "0")
                self.assertEqual(command[command.index("--candidate-index") + 1], "2")
                image = root / "scores" / "plot.png"
                Image.new("RGB", (5, 5), "white").save(image)
                result = {"ok": True, "candidates": [{"candidate_index": 2, "scorePlot": str(image)}]}
                return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")
            with patch.object(tools, "ROOT", root), patch.object(tools.subprocess, "run", side_effect=fake_run):
                arguments = json.dumps({"model_path": "model.cif", "map_path": "map.mrc", "output_dir": "scores", "candidate_index": 2})
                context = ToolContext(context=None, tool_name="run_daq", tool_call_id="test", tool_arguments=arguments)
                result = asyncio.run(tools.run_daq.on_invoke_tool(context, arguments))
            self.assertEqual(json.loads(result[0].text)["candidates"][0]["candidate_index"], 2)


if __name__ == "__main__":
    unittest.main()
