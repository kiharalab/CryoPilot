"""External programs the modeling agent needs, resolved the way its tools do.

The agent resolves PyMOL and ChimeraX to noninteractive executables (cluster
``#!/bin/bash -i`` wrappers are skipped) and VESPER to its built binary. A plain
``shutil.which`` in the server would report those wrappers as usable, so ask
the agent's own code, in its own Python environment.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any

from .config import MODELING_AGENT_ROOT, MODELING_PYTHON, PACKAGED

CACHE_SECONDS = 600
TOOLS = ("pymol", "chimerax", "vesper")
PROBE_ARGUMENT = "--probe-modeling-toolchain"

_PROBE = """
import json, sys
sys.path.insert(0, ".")
result = {}
try:
    import tools, vesper_align
except Exception as exc:
    result = {name: {"ok": False, "error": f"Modeling agent import failed: {exc}"} for name in ("pymol", "chimerax", "vesper")}
else:
    for name, resolve in (("pymol", tools._pymol_executable), ("chimerax", tools._chimerax_executable), ("vesper", vesper_align.executable)):
        try:
            result[name] = {"ok": True, "path": resolve()}
        except Exception as exc:
            result[name] = {"ok": False, "error": str(exc)}
    # Needed only for refinement and workspace search; reported, not required.
    import shutil
    try:
        import refinement
        optional = (
            ("phenix", lambda: refinement.phenix_executable("real_space_refine")),
            ("rosetta", refinement.rosetta_scripts_executable),
            ("rosetta_database", refinement.rosetta_database),
        )
    except Exception as exc:
        optional = ()
        for name in ("phenix", "rosetta", "rosetta_database"):
            result[name] = {"ok": False, "optional": True, "error": f"Refinement import failed: {exc}"}
    for name, resolve in optional:
        try:
            result[name] = {"ok": True, "optional": True, "path": str(resolve())}
        except Exception as exc:
            result[name] = {"ok": False, "optional": True, "error": str(exc)}
    found = shutil.which("rg")
    result["ripgrep"] = {"ok": True, "optional": True, "path": found} if found else {"ok": False, "optional": True, "error": "rg not found on PATH"}
print(json.dumps(result))
"""

_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cached_at = 0.0
_refreshing = False


def _probe() -> dict[str, Any]:
    try:
        # A packaged executable runs _PROBE itself when given this argument.
        command = [MODELING_PYTHON, PROBE_ARGUMENT] if PACKAGED else [MODELING_PYTHON, "-c", _PROBE]
        completed = subprocess.run(
            command,
            cwd=MODELING_AGENT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            check=False,
        )
        lines = completed.stdout.strip().splitlines()
        result = json.loads(lines[-1]) if lines else {}
        if not isinstance(result, dict) or not all(name in result for name in TOOLS):
            raise ValueError((completed.stderr or completed.stdout).strip()[-400:] or "no output")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result = {name: {"ok": False, "error": f"Could not check the modeling toolchain: {exc}"} for name in TOOLS}
    result["checked_at"] = time.time()
    return result


def _refresh() -> None:
    global _cached, _cached_at, _refreshing
    result = _probe()
    with _lock:
        _cached, _cached_at, _refreshing = result, time.monotonic(), False


def status(refresh: bool = False) -> dict[str, Any] | None:
    """Return the cached toolchain status and refresh it in the background.

    Returns None until the first check finishes. ``refresh`` starts a new
    check even when the cached result is recent.
    """

    global _refreshing
    with _lock:
        stale = refresh or _cached is None or time.monotonic() - _cached_at > CACHE_SECONDS
        start = stale and not _refreshing
        if start:
            _refreshing = True
        cached = _cached
    if start:
        threading.Thread(target=_refresh, name="modeling-toolchain", daemon=True).start()
    return cached
