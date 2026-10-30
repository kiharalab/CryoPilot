"""Offline tests for the CryoPilot web app server pieces.

Run from the repository root:
    python -m pytest app/tests -q
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for path in (REPO_ROOT, REPO_ROOT / "reconstruction_agent"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.server import modeling_toolchain  # noqa: E402
from app.server.manifests import ManifestError, build_agent_config, build_manifest, compose_modeling_request  # noqa: E402
from app.server.store import AppStore  # noqa: E402
from app.server.trace import classify  # noqa: E402
from startup_cli import build_developer_instruction, build_user_prompt, bind_manifest_scope  # noqa: E402


def full_form(**overrides):
    form = {
        "project_title": "Apoferritin",
        "parent_directory": "/data/projects",
        "dataset_kind": "movies",
        "dataset_path": "/data/movies/*.tif",
        "pixel_size_A": "0.83",
        "accelerating_voltage_kV": 300,
        "spherical_aberration_mm": "2.7",
        "total_dose_e_per_A2": 50,
        "particle_diameter_A": "130",
        "molecular_mass_kDa": "",
        "expected_symmetry": "",
        "user_goal": "Best map",
    }
    form.update(overrides)
    return form


def test_manifest_matches_cli_schema_and_binds():
    manifest = build_manifest(full_form())
    assert manifest["schema_version"] == 1
    assert manifest["dataset"]["kind"] == "movies"
    assert manifest["dataset"]["eer"] == {"enabled": False, "fractions": None, "upsampling": None}
    assert manifest["acquisition"]["pixel_size_A"] == 0.83
    assert manifest["sample"]["expected_symmetry"] == "C1"
    assert manifest["sample"]["molecular_mass_kDa"] is None
    bound = bind_manifest_scope(manifest, session_id="s1", project_uid="P1", workspace_uid="W1")
    assert "csw.import_movies" in build_developer_instruction(bound)
    assert "Session manifest" in build_user_prompt(bound)


def test_manifest_reports_every_missing_field():
    with pytest.raises(ManifestError) as info:
        build_manifest({"dataset_kind": "particles"})
    errors = info.value.errors
    for key in ("project_title", "parent_directory", "particle_metadata_path", "dataset_path", "pixel_size_A", "user_goal"):
        assert errors[key] == "Required"


def test_manifest_validates_numbers_and_eer():
    with pytest.raises(ManifestError) as info:
        build_manifest(full_form(pixel_size_A="abc", spherical_aberration_mm="-1", eer_enabled=True, eer_fractions="0"))
    errors = info.value.errors
    assert errors["pixel_size_A"] == "Enter a number"
    assert "at least 0" in errors["spherical_aberration_mm"]
    assert errors["eer_fractions"] == "Enter a positive integer"
    assert errors["eer_upsampling"] == "Enter a positive integer"

    manifest = build_manifest(full_form(eer_enabled="true", eer_fractions="40", eer_upsampling="2", gain_reference_path="/g.mrc"))
    assert manifest["dataset"]["eer"] == {"enabled": True, "fractions": 40, "upsampling": 2}
    assert manifest["dataset"]["gain_reference_path"] == "/g.mrc"


def test_particles_manifest_keeps_metadata_path():
    manifest = build_manifest(full_form(dataset_kind="particles", particle_metadata_path="/p.star", dataset_path="/p/*.mrcs"))
    assert manifest["dataset"]["particle_metadata_path"] == "/p.star"
    assert manifest["dataset"]["gain_reference_path"] is None


def test_agent_config_defaults_and_limits():
    config = build_agent_config({"provider": "OpenAI", "reasoning_effort": "High", "rlm_max_depth": "3", "rlm_max_calls": ""})
    assert config == {"provider": "openai", "reasoning_effort": "high", "rlm_max_depth": 3}
    # A Claude model runs on Anthropic unless the form names a provider.
    assert build_agent_config({"model": "claude-sonnet-5-5"}) == {"provider": "anthropic", "model": "claude-sonnet-5-5"}
    assert build_agent_config({"model": "gpt-5.5"})["provider"] == "openai"
    with pytest.raises(ManifestError):
        build_agent_config({"provider": "gemini"})
    with pytest.raises(ManifestError):
        build_agent_config({"rlm_max_depth": "0"})


def test_modeling_request_lists_workspace_inputs():
    text = compose_modeling_request(
        {"objective": "Validate", "map_path": "data/emd.map", "model_path": "workspaces/p/data/m.pdb", "contour_level": "0.5"},
        "workspaces/p",
    )
    assert text.startswith("Validate")
    assert "- Density map: workspaces/p/data/emd.map" in text
    assert "- Model: workspaces/p/data/m.pdb" in text
    assert "contour level: 0.5" in text


def test_modeling_toolchain_reports_agent_resolution(monkeypatch):
    probe = {"pymol": {"ok": True, "path": "/opt/pymol/bin/pymol"}, "chimerax": {"ok": False, "error": "missing"}, "vesper": {"ok": True, "path": "/v"}}

    def fake_run(command, **kwargs):
        assert kwargs["start_new_session"] and kwargs["stdin"] is modeling_toolchain.subprocess.DEVNULL
        return modeling_toolchain.subprocess.CompletedProcess(command, 0, stdout="noise\n" + json.dumps(probe), stderr="")

    monkeypatch.setattr(modeling_toolchain.subprocess, "run", fake_run)
    result = modeling_toolchain._probe()
    assert result["pymol"]["path"] == "/opt/pymol/bin/pymol"
    assert result["chimerax"] == {"ok": False, "error": "missing"}


def test_modeling_toolchain_failure_marks_every_tool(monkeypatch):
    monkeypatch.setattr(
        modeling_toolchain.subprocess,
        "run",
        lambda command, **kwargs: modeling_toolchain.subprocess.CompletedProcess(command, 1, stdout="", stderr="ImportError: agents"),
    )
    result = modeling_toolchain._probe()
    assert all(not result[name]["ok"] and "ImportError" in result[name]["error"] for name in modeling_toolchain.TOOLS)


def test_classify_tool_call_and_result():
    call = classify({"type": "function_call", "name": "run_code", "call_id": "c1", "arguments": json.dumps({"code": "print(1)"})}, "model")
    assert call["kind"] == "tool_call" and call["code"] == "print(1)" and call["arguments"] == {}
    result = classify(
        {"type": "function_call_output", "call_id": "c1", "output": json.dumps({"ok": False, "stdout": "x", "traceback": "Trace"})},
        "tool",
    )
    assert result["kind"] == "tool_result" and result["ok"] is False and result["traceback"] == "Trace"


def test_classify_messages_and_images():
    assert classify({"role": "user", "content": "hi"}, "bootstrap")["bootstrap"] is True
    image = classify(
        {"role": "user", "content": [{"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
        "tool",
    )
    assert image["kind"] == "image" and image["images"] == [{"index": 1, "mime": "image/png"}]
    assistant = classify({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}, "model")
    assert assistant["kind"] == "assistant" and assistant["text"] == "done"
    reasoning = classify({"type": "reasoning", "summary": [{"type": "summary_text", "text": "why"}], "encrypted_content": "x"}, "model")
    assert reasoning["kind"] == "reasoning" and reasoning["text"] == "why" and reasoning["encrypted"]
    developer = classify({"role": "developer", "content": "[SESSION RESUME]\nhi"}, "resume")
    assert developer["kind"] == "resume"


def test_store_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        store = AppStore(Path(tmp) / "app.sqlite3")
        project = store.create_project(title="My Project!", description="d", parent_directory="/p")
        assert project["slug"] == "my-project"
        again = store.create_project(title="My Project!")
        assert again["slug"] == "my-project-2"
        session = store.create_session(project_id=project["id"], kind="modeling", title="t", config={"model": "m"})
        store.append_modeling_event(session["id"], "user", {"text": "hello"})
        store.append_modeling_event(session["id"], "answer", {"text": "hi"})
        events = store.list_modeling_events(session["id"])
        assert [event["kind"] for event in events] == ["user", "answer"]
        assert store.list_modeling_events(session["id"], after=events[0]["id"])[0]["kind"] == "answer"
        store.update_session(session["id"], status="error", last_error="boom")
        assert store.get_session(session["id"])["last_error"] == "boom"
        listed = store.list_projects()
        assert listed[0]["session_counts"] == {"modeling": 1}
        store.delete_project(project["id"])
        assert store.list_modeling_events(session["id"]) == []
        store.close()
