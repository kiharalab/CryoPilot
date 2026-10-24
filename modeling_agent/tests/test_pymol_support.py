from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.tool_context import ToolContext
from PIL import Image

import tools


class PyMOLLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pymol test ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def executable(self, name: str, contents: str) -> Path:
        path = self.root / name
        path.write_text(contents)
        path.chmod(0o755)
        return path

    def test_interactive_wrapper_resolves_noninteractive_module_executable(self):
        wrapper = self.executable("wrapper", "#!/bin/bash -i\nmodule load pymol\n")
        binary = self.executable("binary", "#!/bin/bash\nexit 0\n")
        with patch.dict(os.environ, {"PYMOL_EXECUTABLE": "", "PYMOL_EXE": ""}), \
             patch.object(tools.shutil, "which", return_value=str(wrapper)), \
             patch.object(tools.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=f"module banner\n{binary}\n", stderr=""
             )) as launched:
            self.assertEqual(tools._pymol_executable(), str(binary))

        self.assertEqual(launched.call_args.args[0][:2], ["/bin/bash", "-lc"])
        self.assertIn("module load pymol", launched.call_args.args[0][2])
        self.assertTrue(launched.call_args.kwargs["start_new_session"])
        self.assertEqual(launched.call_args.kwargs["stdin"], tools.subprocess.DEVNULL)

    def test_direct_executable_skips_module_lookup(self):
        binary = self.executable("binary", "#!/bin/bash\nexit 0\n")
        with patch.dict(os.environ, {"PYMOL_EXECUTABLE": "", "PYMOL_EXE": ""}), \
             patch.object(tools.shutil, "which", return_value=str(binary)), \
             patch.object(tools.subprocess, "run", side_effect=AssertionError("unexpected module lookup")):
            self.assertEqual(tools._pymol_executable(), str(binary))

    def test_configured_interactive_wrapper_is_rejected(self):
        wrapper = self.executable("wrapper", "#!/bin/bash -i\nexit 0\n")
        with patch.dict(os.environ, {"PYMOL_EXECUTABLE": str(wrapper)}):
            with self.assertRaisesRegex(RuntimeError, "interactive Bash"):
                tools._pymol_executable()

    def test_missing_module_fails_without_running_interactive_wrapper(self):
        wrapper = self.executable("wrapper", "#!/bin/bash -i\nexit 0\n")
        with patch.dict(os.environ, {"PYMOL_EXECUTABLE": "", "PYMOL_EXE": ""}), \
             patch.object(tools.shutil, "which", return_value=str(wrapper)), \
             patch.object(tools.subprocess, "run", return_value=SimpleNamespace(
                 returncode=1, stdout="", stderr="module not found"
             )):
            with self.assertRaisesRegex(RuntimeError, "noninteractive PyMOL"):
                tools._pymol_executable()

    def test_render_pdb_uses_isolated_noninteractive_launcher(self):
        (self.root / "model.pdb").write_text("MODEL\nENDMDL\n")
        arguments = json.dumps({"pdb_path": "model.pdb", "output_path": "render.png"})
        context = ToolContext(
            context=None, tool_name="render_pdb", tool_call_id="test", tool_arguments=arguments
        )

        def fake_run(command, **kwargs):
            self.assertEqual(command[:4], ["/path/to/pymol", "-cq", "-d", "set max_threads, 2"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["stdin"], tools.subprocess.DEVNULL)
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            Image.new("RGB", (5, 5), "white").save(self.root / "render.png")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-secret", "CRYOPILOT_PYMOL_THREADS": "2"}), \
             patch.object(tools, "ROOT", self.root), \
             patch.object(tools, "_pymol_executable", return_value="/path/to/pymol"), \
             patch.object(tools.subprocess, "run", side_effect=fake_run):
            result = tools.render_pdb.on_invoke_tool(context, arguments)
            if hasattr(result, "__await__"):
                import asyncio
                result = asyncio.run(result)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
