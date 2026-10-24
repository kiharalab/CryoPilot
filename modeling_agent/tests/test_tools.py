from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tools
from tools import ROOT, _run_bash


class BashToolTests(unittest.TestCase):
    def setUp(self):
        # Keep tests independent of the host's PyMOL/ChimeraX modules.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.launchers = Path(temporary.name)
        patcher = patch.object(tools, "_LAUNCHER_DIRECTORY", self.launchers)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_stdout_stderr_and_exit_code(self):
        result = json.loads(
            _run_bash("printf 'hello'; printf 'warning' >&2; exit 7")
        )

        self.assertEqual(result["exit_code"], 7)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["working_directory"], str(ROOT))
        self.assertEqual(result["stdout"], "hello")
        self.assertEqual(result["stderr"], "warning")

    def test_rejects_working_directory_outside_workspace(self):
        with self.assertRaisesRegex(ValueError, "must stay inside"):
            _run_bash("pwd", working_directory="..")

    def test_scrubs_credentials_from_subprocess_environment(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "do-not-expose",
                "CRYOZETA_PASSWORD": "do-not-expose",
                "BASH_TOOL_VISIBLE": "visible",
            },
            clear=False,
        ):
            result = json.loads(
                _run_bash(
                    "printf '%s|%s|%s' \"${OPENAI_API_KEY-unset}\" "
                    "\"${CRYOZETA_PASSWORD-unset}\" \"$BASH_TOOL_VISIBLE\""
                )
            )

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "unset|unset|visible")

    def test_runs_detached_from_terminal_in_new_session(self):
        result = json.loads(_run_bash(
            'if [ -t 0 ]; then echo tty; else echo no-tty; fi; '
            f'{shlex.quote(sys.executable)} -c "import os; print(os.getsid(0))"'
        ))

        tty, session = result["stdout"].split()
        self.assertEqual(tty, "no-tty")
        self.assertNotEqual(int(session), os.getsid(0))

    def test_timeout_kills_background_children(self):
        started = time.monotonic()
        result = json.loads(_run_bash("sleep 30 & sleep 30", timeout_seconds=1))

        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - started, 10)

    def test_bare_pymol_uses_noninteractive_executable(self):
        binary = self.launchers / "real pymol"
        binary.write_text('#!/bin/sh\necho "real:$*"\n')
        binary.chmod(0o755)
        with patch.dict(os.environ, {"CRYOPILOT_PYMOL_THREADS": "4"}), \
             patch.object(tools, "_LAUNCHER_DIRECTORY", None), \
             patch.object(tools, "_pymol_executable", return_value=str(binary)), \
             patch.object(tools, "_chimerax_executable", side_effect=RuntimeError("absent")):
            result = json.loads(_run_bash("pymol -cq 'a b'"))
            launchers = tools._LAUNCHER_DIRECTORY
        self.addCleanup(shutil.rmtree, launchers)

        self.assertEqual(result["stdout"], "real:-d set max_threads, 4 -cq a b\n")
        self.assertEqual(sorted(path.name for path in launchers.iterdir()), ["pymol"])



if __name__ == "__main__":
    unittest.main()
