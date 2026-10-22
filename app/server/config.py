"""Paths and environment for the CryoAgent web application.

Everything the app persists lives under ``APP_HOME`` (``~/.cryoagent/app`` by
default). The reconstruction agent keeps using the harness session database
(``~/.cryoagent/sessions.sqlite3`` by default) so sessions started from the
terminal and from the web app are interchangeable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# A packaged (Nuitka) build compiles every module into one executable; the
# modeling agent's modules sit at the top level next to the harness, and the
# executable stands in for the Python interpreter of both agents.
PACKAGED = "__compiled__" in globals()

APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent
MODELING_AGENT_ROOT = REPO_ROOT if PACKAGED else REPO_ROOT / "modeling_agent"
FRONTEND_DIR = APP_DIR / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist"
DRIVERS_DIR = APP_DIR / "server" / "drivers"

APP_HOME = Path(os.environ.get("CRYOAGENT_APP_HOME", "~/.cryoagent/app")).expanduser()
PROJECTS_DIR = APP_HOME / "projects"
APP_DB_PATH = APP_HOME / "app.sqlite3"
MODELING_MEMORY_DB = APP_HOME / "modeling_sessions.sqlite3"
HARNESS_SESSION_DB = Path(
    os.environ.get("CRYOAGENT_SESSION_DB", "~/.cryoagent/sessions.sqlite3")
).expanduser()

# Modeling tools refuse any path outside their sandbox root, so each project
# gets a workspace folder inside it. From source the sandbox is the modeling
# agent directory; a packaged build cannot write next to its code, so the
# sandbox moves under APP_HOME and reaches the agent as MODELING_AGENT_SANDBOX.
MODELING_SANDBOX_ROOT = APP_HOME / "modeling" if PACKAGED else MODELING_AGENT_ROOT
MODELING_WORKSPACES_DIR = MODELING_SANDBOX_ROOT / "workspaces"

RECONSTRUCTION_PYTHON = sys.executable
_venv_python = MODELING_AGENT_ROOT / ".venv" / "bin" / "python"
MODELING_PYTHON = str(_venv_python) if _venv_python.exists() and not PACKAGED else sys.executable

DEFAULT_RECONSTRUCTION_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
DEFAULT_REASONING_EFFORT = os.environ.get(
    "OPENAI_REASONING_EFFORT", os.environ.get("REASONING_EFFORT", "xhigh")
)
DEFAULT_MODELING_MODEL = os.environ.get("MODELING_MODEL", "gpt-5.5")
OLLAMA_MODEL = "gemma4:12b-mlx"


def ensure_directories() -> None:
    # The app's data (transcripts, console logs, uploads, copied maps) stays
    # private to the user on a shared machine, whatever the umask. A process
    # umask would also reach the agents, which write into CryoSPARC project
    # folders other accounts must read, so only these folders are closed.
    for path in (APP_HOME, PROJECTS_DIR):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass
    MODELING_WORKSPACES_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
