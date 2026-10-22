"""CryoZeta (Kihara Lab EM server) login check for the Settings page.

Reuses the modeling agent's own client so the app authenticates exactly the
way the agent's tools do. Credentials are passed in; nothing is persisted.
"""

from __future__ import annotations

import sys
from typing import Any

from .config import MODELING_AGENT_ROOT

if str(MODELING_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(MODELING_AGENT_ROOT))

from cryozeta_client import DEFAULT_BASE_URL, CryoZetaClient, CryoZetaError  # noqa: E402

__all__ = ["DEFAULT_BASE_URL", "CryoZetaError", "test_login"]


def test_login(base_url: str, username: str, password: str) -> dict[str, Any]:
    client = CryoZetaClient(base_url or DEFAULT_BASE_URL)
    user = client.login(username, password)
    return {"ok": True, "username": user.get("username") or username}
