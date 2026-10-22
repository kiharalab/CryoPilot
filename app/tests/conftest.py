"""Point the app at throwaway folders before any test imports the server.

The server's config reads CRYOAGENT_APP_HOME and CRYOAGENT_SESSION_DB at import
time, so this runs first and keeps every test away from ~/.cryoagent. The
access check reads CRYOPILOT_ALLOWED_HOSTS at import time too; TestClient
sends ``Host: testserver``.
"""

from __future__ import annotations

import os
import tempfile

_home = tempfile.mkdtemp(prefix="cryopilot-tests-")
os.environ.setdefault("CRYOAGENT_APP_HOME", _home)
os.environ.setdefault("CRYOAGENT_SESSION_DB", os.path.join(_home, "sessions.sqlite3"))
os.environ.setdefault("CRYOPILOT_ALLOWED_HOSTS", "testserver")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _private_modeling_workspaces(monkeypatch, tmp_path_factory):
    """Projects made by tests get their modeling workspace in a temporary folder.

    From source the workspaces live in modeling_agent/workspaces/, outside the
    app home, so without this every test project would leave a folder there.
    """

    from app.server import files

    sandbox = tmp_path_factory.mktemp("modeling-sandbox")
    monkeypatch.setattr(files, "MODELING_SANDBOX_ROOT", sandbox)
    monkeypatch.setattr(files, "MODELING_WORKSPACES_DIR", sandbox / "workspaces")
