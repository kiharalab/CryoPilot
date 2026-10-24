from __future__ import annotations

import json
import os
import runpy
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import tools


class ChimeraXLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chimerax test ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # Exercise configuration transport without any structure or map input.
        self.support = self.root / "probe.py"
        self.support.write_text(
            "import json\n"
            "from pathlib import Path\n"
            "config = json.loads(Path(__file__).with_name('config.json').read_text())\n"
            "result = dict(config, ok=True, has_session='session' in globals())\n"
            "Path(config['result_path']).write_text(json.dumps(result))\n"
        )
        self.config = {
            "operation": "probe",
            "model_path": "input model.cif",
            "map_path": "input map.mrc",
            "output_path": "output map.mrc",
        }

    def launch(self):
        with patch.object(tools, "ROOT", self.root), patch.object(tools, "SUPPORT_SCRIPT", self.support):
            return tools._run_chimerax_support(self.config)

    def assert_config_delivered(self, result):
        self.assertTrue(result["ok"])
        self.assertTrue(result["has_session"])
        for key in ("model_path", "map_path", "output_path"):
            self.assertEqual(result[key], str(self.root / self.config[key]))
        self.assertEqual(result["operation"], "probe")
        self.assertFalse(Path(result["result_path"]).exists(), "staging directory should be cleaned up")

    def test_interactive_shell_wrapper_resolves_module_binary(self):
        wrapper = self.root / "wrapper" / "chimerax"
        wrapper.parent.mkdir()
        wrapper.write_text("#!/bin/bash -i\nmodule load chimerax\nexec chimerax $*\n")
        wrapper.chmod(0o755)
        binary = self.root / "module" / "chimerax"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)

        with patch.dict(os.environ, {"CHIMERAX_EXECUTABLE": ""}), \
             patch.object(tools.shutil, "which", side_effect=lambda name: str(wrapper) if name == "chimerax" else None), \
             patch.object(tools.Path, "glob", return_value=[]), \
             patch.object(tools.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=f"login banner\n{binary}\n", stderr=""
             )) as launched:
            self.assertEqual(tools._chimerax_executable(), str(binary))

        self.assertEqual(launched.call_args.args[0][:2], ["/bin/bash", "-lc"])
        self.assertIn("module load chimerax", launched.call_args.args[0][2])
        self.assertTrue(launched.call_args.kwargs["start_new_session"])

    def test_interactive_shell_wrapper_without_module_fails_closed(self):
        wrapper = self.root / "chimerax"
        wrapper.write_text("#!/bin/bash -i\nexit 0\n")
        wrapper.chmod(0o755)
        with patch.dict(os.environ, {"CHIMERAX_EXECUTABLE": ""}), \
             patch.object(tools.shutil, "which", return_value=str(wrapper)), \
             patch.object(tools.Path, "glob", return_value=[]), \
             patch.object(tools.subprocess, "run", return_value=SimpleNamespace(
                 returncode=1, stdout="", stderr="module not found"
             )):
            with self.assertRaisesRegex(RuntimeError, "noninteractive binary"):
                tools._chimerax_executable()

    def test_configured_binary_skips_module_and_rejects_interactive_wrapper(self):
        binary = self.root / "chimerax"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        with patch.dict(os.environ, {"CHIMERAX_EXECUTABLE": str(binary)}), \
             patch.object(tools.subprocess, "run", side_effect=AssertionError("unexpected module lookup")):
            self.assertEqual(tools._chimerax_executable(), str(binary))
        binary.write_text("#!/bin/bash -i\nexit 0\n")
        with patch.dict(os.environ, {"CHIMERAX_EXECUTABLE": str(binary)}):
            with self.assertRaisesRegex(RuntimeError, "interactive Bash"):
                tools._chimerax_executable()

    def test_cluster_launcher_that_splits_arguments(self):
        runtime = self.root / "fake_chimerax.py"
        runtime.write_text(
            "import runpy, shlex, sys\n"
            "assert '--nogui' in sys.argv and '--exit' in sys.argv\n"
            "script_args = shlex.split(sys.argv[sys.argv.index('--script') + 1])\n"
            "sys.argv = script_args\n"
            "runpy.run_path(script_args[0], init_globals={'session': object()})\n"
        )
        launcher = self.root / "chimerax"
        # Deliberately reproduce Zebra's unquoted argument forwarding.
        launcher.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(runtime))} $*\n"
        )
        launcher.chmod(0o755)
        with patch.object(tools, "_chimerax_executable", return_value=str(launcher)):
            self.assert_config_delivered(self.launch())

    def test_missing_result_and_nonzero_exit_report_launch_diagnostics(self):
        for returncode, reason in ((0, "no result file"), (3, "exit code 3")):
            with self.subTest(returncode=returncode), \
                 patch.object(tools, "_chimerax_executable", return_value="chimerax"), \
                 patch.object(tools.subprocess, "run", return_value=SimpleNamespace(
                     returncode=returncode, stdout="launcher output", stderr="script error"
                 )) as launched:
                with self.assertRaisesRegex(RuntimeError, reason) as raised:
                    self.launch()
                self.assertIn("launcher output", str(raised.exception))
                self.assertIn("script error", str(raised.exception))
                self.assertTrue(launched.call_args.kwargs["start_new_session"])

    @unittest.skipUnless(os.environ.get("CRYOPILOT_TEST_CHIMERAX") == "1", "optional installed ChimeraX launcher test")
    def test_installed_chimerax_launches_script_with_config(self):
        self.assert_config_delivered(self.launch())


class ChimeraXConfigTests(unittest.TestCase):
    def test_bridge_reads_staged_config_or_explicit_argument(self):
        commands = ModuleType("chimerax.core.commands")
        commands.run = lambda session, command: [SimpleNamespace(id_string="1")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "chimerax_support.py"
            script.write_text(tools.SUPPORT_SCRIPT.read_text())
            for filename, arguments in (("config.json", []), ("manual config.json", [str(root / "manual config.json")])):
                config = {"operation": "unsupported", "model_path": "unused.cif", "result_path": str(root / "result.json")}
                (root / filename).write_text(json.dumps(config))
                with self.subTest(filename=filename), \
                     patch.object(sys, "argv", [str(script), *arguments]), \
                     patch.dict(sys.modules, {"chimerax.core.commands": commands}):
                    runpy.run_path(str(script), init_globals={"session": object()})
                result = json.loads((root / "result.json").read_text())
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "Unknown ChimeraX support operation.")
                (root / "result.json").unlink()
                (root / filename).unlink()


if __name__ == "__main__":
    unittest.main()
