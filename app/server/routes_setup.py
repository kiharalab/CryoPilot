"""Health, the license, first-run setup, settings, credentials, processing guides, and updates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import anthropic
import openai
from fastapi import APIRouter, Request

from ..version import VERSION
from . import alerts, cryosparc_bridge, cryozeta_bridge, guides, modeling_toolchain, pricing, updates
from .config import HARNESS_SESSION_DB, MODELING_PYTHON, REPO_ROOT
from .state import (
    alert_sender,
    alerts_view,
    RECONSTRUCTION_EFFORTS,
    RECONSTRUCTION_SUPPORTED,
    REASONING_EFFORTS,
    SYSTEM,
    anthropic_key,
    api_error,
    credentials,
    cryosparc_credentials,
    cryosparc_settings,
    cryozeta_credentials,
    cryozeta_settings,
    defaults,
    experimental_settings,
    openai_key,
    parse_spending_limit,
    processes,
    processing_guide,
    store,
)

router = APIRouter()

# Accepting a different license (a new ID) asks everyone to accept again.
LICENSE_ID = "PolyForm-Noncommercial-1.0.0"
LICENSE_NAME = "PolyForm Noncommercial License 1.0.0"
LICENSE_FILE = REPO_ROOT / "LICENSE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _openai_view() -> dict[str, Any]:
    return {"configured": bool(openai_key()), "source": credentials.source("openai_api_key")}


def _anthropic_view() -> dict[str, Any]:
    return {"configured": bool(anthropic_key()), "source": credentials.source("anthropic_api_key")}


def _license_view() -> dict[str, Any]:
    accepted = store.get_setting("license_accepted") or {}
    current = accepted.get("license") == LICENSE_ID
    return {"id": LICENSE_ID, "name": LICENSE_NAME, "accepted": current, "accepted_at": accepted.get("accepted_at") if current else None}


def setup_view() -> dict[str, Any]:
    return {
        "version": VERSION,
        "license": _license_view(),
        "onboarded": bool(store.get_setting("onboarded", False)),
        "platform": {"system": SYSTEM, "reconstruction_supported": RECONSTRUCTION_SUPPORTED},
        "openai": _openai_view(),
        "anthropic": _anthropic_view(),
        "cryosparc": cryosparc_settings(),
        "cryozeta": cryozeta_settings(),
        "processing_guide": processing_guide(),
        "remember_credentials": credentials.remember,
        "defaults": defaults(),
        "modeling_tools": modeling_toolchain.status(),
        "alerts": alerts_view(),
        "experimental": experimental_settings(),
        "prices_as_of": pricing.PRICES_AS_OF,
    }


# ------------------------------------------------------------------ health
@router.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "openai_key_set": bool(openai_key()),
        "anthropic_key_set": bool(anthropic_key()),
        "harness_db": str(HARNESS_SESSION_DB),
        "harness_db_exists": HARNESS_SESSION_DB.exists(),
        "modeling_python": MODELING_PYTHON,
        **setup_view(),
    }


# ----------------------------------------------------------------- updates
def _update_view() -> dict[str, Any]:
    return {**updates.status(), "agents_running": processes.running()}


@router.get("/api/update")
def get_update() -> dict[str, Any]:
    return _update_view()


@router.post("/api/update/check")
def check_for_update() -> dict[str, Any]:
    if updates.checks_off():
        raise api_error(409, "Update checks are turned off on this server (CRYOPILOT_NO_UPDATE_CHECK=1).")
    try:
        updates.check_now()
    except updates.UpdateError as exc:
        raise api_error(502, str(exc))
    return _update_view()


@router.post("/api/update/install")
def install_update() -> dict[str, Any]:
    try:
        updates.start_install()
    except updates.UpdateError as exc:
        raise api_error(409, str(exc))
    return _update_view()


@router.post("/api/update/restart")
def restart_for_update() -> dict[str, Any]:
    # A restart stops the agents, so it waits until they are done.
    running = processes.running()
    if running:
        raise api_error(409, f"{running} agent{'' if running == 1 else 's'} still running. Restart when they are done.")
    try:
        updates.restart()
    except updates.UpdateError as exc:
        raise api_error(409, str(exc))
    return _update_view()


# ----------------------------------------------------------------- license
@router.get("/api/license")
def get_license() -> dict[str, Any]:
    try:
        text = LICENSE_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise api_error(500, f"Could not read the license: {exc}")
    return {**_license_view(), "text": text}


@router.post("/api/license/accept")
def accept_license() -> dict[str, Any]:
    store.set_setting("license_accepted", {"license": LICENSE_ID, "accepted_at": _now()})
    return setup_view()


# ------------------------------------------------------------------- setup
@router.get("/api/setup")
def get_setup() -> dict[str, Any]:
    return setup_view()


@router.post("/api/setup/complete")
def complete_setup() -> dict[str, Any]:
    store.set_setting("onboarded", True)
    return setup_view()


# ---------------------------------------------------------------- settings
@router.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return setup_view()


@router.put("/api/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    body = await request.json()

    if "remember_credentials" in body:
        credentials.set_remember(bool(body["remember_credentials"]))

    openai = body.get("openai") or {}
    if openai.get("api_key"):
        credentials.set("openai_api_key", str(openai["api_key"]))
    if openai.get("clear"):
        credentials.set("openai_api_key", None)

    claude = body.get("anthropic") or {}
    if claude.get("api_key"):
        credentials.set("anthropic_api_key", str(claude["api_key"]))
    if claude.get("clear"):
        credentials.set("anthropic_api_key", None)

    cryosparc = body.get("cryosparc") or {}
    if cryosparc:
        saved = store.get_setting("cryosparc", {}) or {}
        changed = False
        for key in ("url", "email"):
            if key in cryosparc:
                value = str(cryosparc[key] or "").strip()
                changed = changed or value != saved.get(key)
                saved[key] = value
        if cryosparc.get("password"):
            credentials.set("cryosparc_password", str(cryosparc["password"]))
            changed = True
        if cryosparc.get("clear_password"):
            credentials.set("cryosparc_password", None)
            changed = True
        if changed:
            saved.pop("verified_at", None)
        store.set_setting("cryosparc", saved)

    cryozeta = body.get("cryozeta") or {}
    if cryozeta:
        saved = store.get_setting("cryozeta", {}) or {}
        changed = False
        for key in ("base_url", "username"):
            if key in cryozeta:
                value = str(cryozeta[key] or "").strip()
                changed = changed or value != saved.get(key)
                saved[key] = value
        if cryozeta.get("password"):
            credentials.set("cryozeta_password", str(cryozeta["password"]))
            changed = True
        if cryozeta.get("clear_password"):
            credentials.set("cryozeta_password", None)
            changed = True
        if changed:
            saved.pop("verified_at", None)
        store.set_setting("cryozeta", saved)

    if "processing_guide" in body:
        choice = str(body["processing_guide"] or "").strip().lower()
        if choice not in guides.GUIDE_FILES:
            raise api_error(422, f"Unknown processing guide: {choice}")
        store.set_setting("processing_guide", choice)

    if "defaults" in body:
        incoming = body.get("defaults") or {}
        saved = store.get_setting("defaults", {}) or {}
        for key in ("reconstruction_model", "modeling_model"):
            if key in incoming:
                saved[key] = str(incoming[key] or "").strip()
        for key, allowed in (("reconstruction_effort", RECONSTRUCTION_EFFORTS), ("modeling_effort", REASONING_EFFORTS)):
            if key in incoming:
                value = str(incoming[key] or "").strip().lower()
                if value and value not in allowed:
                    raise api_error(422, f"Unknown reasoning effort: {value}")
                saved[key] = value
        if "modeling_max_turns" in incoming:
            try:
                turns = int(incoming["modeling_max_turns"])
            except (TypeError, ValueError):
                raise api_error(422, "Maximum turns must be a whole number")
            if not 1 <= turns <= 5000:
                raise api_error(422, "Maximum turns must be between 1 and 5000")
            saved["modeling_max_turns"] = turns
        if "spending_limit_usd" in incoming:
            try:
                saved["spending_limit_usd"] = parse_spending_limit(incoming["spending_limit_usd"])
            except ValueError as exc:
                raise api_error(422, str(exc))
        store.set_setting("defaults", saved)

    if isinstance(body.get("experimental"), dict) and "enabled" in body["experimental"]:
        store.set_setting("experimental", {"enabled": bool(body["experimental"]["enabled"])})

    if isinstance(body.get("alerts"), dict):
        try:
            changed = alerts.apply_changes(store.get_setting("alerts", {}) or {}, body["alerts"], credentials.set)
        except alerts.AlertSettingsError as exc:
            raise api_error(422, str(exc))
        store.set_setting("alerts", changed)

    return setup_view()


@router.post("/api/settings/alerts/test")
def test_alerts() -> dict[str, Any]:
    results = alert_sender.test()
    if not results:
        raise api_error(400, "Add a webhook or an email address first.")
    return {"results": results, "alerts": alerts_view()}


# ---------------------------------------------------------- connection tests
@router.post("/api/settings/openai/test")
def test_openai() -> dict[str, Any]:
    key = openai_key()
    if not key:
        raise api_error(400, "Enter an OpenAI API key first.")
    try:
        models = sorted(model.id for model in openai.OpenAI(api_key=key, timeout=20).models.list())
    except openai.AuthenticationError:
        raise api_error(502, "OpenAI rejected this API key.")
    except openai.APIStatusError as exc:
        raise api_error(502, f"OpenAI returned HTTP {exc.status_code}.")
    except openai.APIConnectionError as exc:
        # The SDK's own text is only "Connection error."; the cause says why.
        raise api_error(502, f"Could not reach OpenAI: {exc.__cause__ or exc}")
    default = defaults()["reconstruction_model"]
    return {"ok": True, "model_count": len(models), "default_model_available": default in models, "models": models}


@router.post("/api/settings/anthropic/test")
def test_anthropic() -> dict[str, Any]:
    key = anthropic_key()
    if not key:
        raise api_error(400, "Enter an Anthropic API key first.")
    try:
        models = sorted(model.id for model in anthropic.Anthropic(api_key=key, timeout=20).models.list(limit=100))
    except anthropic.AuthenticationError:
        raise api_error(502, "Anthropic rejected this API key.")
    except anthropic.APIStatusError as exc:
        raise api_error(502, f"Anthropic returned HTTP {exc.status_code}.")
    except anthropic.APIConnectionError as exc:
        raise api_error(502, f"Could not reach Anthropic: {exc}")
    default = defaults()["reconstruction_model"]
    return {"ok": True, "model_count": len(models), "default_model_available": default in models, "models": models}


@router.post("/api/settings/cryosparc/test")
def test_cryosparc() -> dict[str, Any]:
    url, email, password = cryosparc_credentials()
    try:
        result = cryosparc_bridge.test_connection(url, email, password)
    except Exception as exc:
        raise api_error(502, f"Could not sign in to CryoSPARC at {url}: {exc}")
    saved = store.get_setting("cryosparc", {}) or {}
    saved["verified_at"] = _now()
    store.set_setting("cryosparc", saved)
    return result


@router.post("/api/settings/cryozeta/test")
def test_cryozeta() -> dict[str, Any]:
    base_url, username, password = cryozeta_credentials()
    try:
        result = cryozeta_bridge.test_login(base_url, username, password)
    except (cryozeta_bridge.CryoZetaError, ValueError) as exc:
        raise api_error(502, f"CryoZeta sign-in failed: {exc}")
    except Exception as exc:
        raise api_error(502, f"Could not reach CryoZeta: {exc}")
    saved = store.get_setting("cryozeta", {}) or {}
    saved["verified_at"] = _now()
    store.set_setting("cryozeta", saved)
    return result


@router.post("/api/settings/modeling-tools/refresh")
def refresh_modeling_tools() -> dict[str, Any]:
    return {"modeling_tools": modeling_toolchain.status(refresh=True)}


# ------------------------------------------------------------------ guides
@router.get("/api/guides")
def list_guides() -> dict[str, Any]:
    return {"guides": guides.list_guides(), "selected": processing_guide(), "default": guides.DEFAULT_GUIDE}


@router.get("/api/guides/{guide_id}")
def get_guide(guide_id: str) -> dict[str, Any]:
    try:
        return guides.read_guide(guide_id)
    except KeyError:
        raise api_error(404, f"Unknown processing guide: {guide_id}")
    except OSError as exc:
        raise api_error(404, f"Could not read the guide: {exc}")
