"""Tests for the access checks, agent environments, session resume, and server odds and ends.

Run from the repository root:
    .venv/bin/python -m pytest app/tests/test_auth_and_core.py -q
"""

from __future__ import annotations

import json
import os
import queue
import socket
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.server import config  # noqa: E402

if config.APP_HOME == Path("~/.cryopilot/app").expanduser():
    pytest.skip("the app home is the user's real one; run with CRYOPILOT_APP_HOME set", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from app import serve  # noqa: E402
from app.server import auth, files, main, processes as processes_module, routes_setup  # noqa: E402
from app.server.credentials import Credentials  # noqa: E402
from app.server.main import app  # noqa: E402
from app.server import state  # noqa: E402
from app.server.state import bus, defaults, driver_env, processes, store  # noqa: E402

HEADERS = {"Authorization": f"Bearer {auth.TOKEN}"}


@pytest.fixture(autouse=True)
def auth_state(monkeypatch):
    """Every test starts signed-out-but-allowed and leaves no pause or used launch code behind."""

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "_paused_until", 0.0)
    monkeypatch.setattr(auth, "_launch_code_used", False)


@pytest.fixture
def client():
    return TestClient(app, headers=HEADERS)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Keep project workspaces out of the repository's modeling_agent folder."""

    monkeypatch.setattr(files, "MODELING_SANDBOX_ROOT", tmp_path)
    monkeypatch.setattr(files, "MODELING_WORKSPACES_DIR", tmp_path / "workspaces")
    return tmp_path


def make_project(title: str) -> dict:
    return store.create_project(title=title)


# ------------------------------------------------------------ host and origin
@pytest.mark.parametrize("auth_enabled", [True, False])
def test_requests_for_another_host_are_refused_in_both_modes(monkeypatch, auth_enabled):
    monkeypatch.setattr(auth, "AUTH_ENABLED", auth_enabled)
    rebound = TestClient(app, base_url="http://evil.example:8790", headers=HEADERS)
    response = rebound.get("/api/setup")
    assert response.status_code == 403
    # A refused host is not a sign-out: the app must not drop to the sign-in gate.
    assert "auth_required" not in response.json()
    assert rebound.post("/api/projects", json={"title": "Rebound"}).status_code == 403
    assert rebound.get("/").status_code == 403


@pytest.mark.parametrize("base_url", ["http://localhost:9000", "http://127.0.0.1:8790", "http://[::1]:8790"])
def test_local_host_names_are_accepted_on_any_port(base_url):
    assert TestClient(app, base_url=base_url, headers=HEADERS).get("/api/setup").status_code == 200


def test_bind_host_and_allowed_hosts_extend_the_list(monkeypatch):
    lab = TestClient(app, base_url="http://zebra.lab:8790", headers=HEADERS)
    assert lab.get("/api/setup").status_code == 403
    monkeypatch.setattr(auth, "BIND_HOST", "zebra.lab")
    assert lab.get("/api/setup").status_code == 200
    monkeypatch.setattr(auth, "BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(auth, "ALLOWED_HOSTS", {"zebra.lab"})
    assert lab.get("/api/setup").status_code == 200
    # Listening on every interface is the user's choice; only the token applies then.
    monkeypatch.setattr(auth, "ALLOWED_HOSTS", set())
    monkeypatch.setattr(auth, "BIND_HOST", "0.0.0.0")
    assert TestClient(app, base_url="http://anything.example", headers=HEADERS).get("/api/setup").status_code == 200


@pytest.mark.parametrize("auth_enabled", [True, False])
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://127.0.0.1:3000"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_changes_from_other_origins_are_refused(client, monkeypatch, auth_enabled, headers):
    monkeypatch.setattr(auth, "AUTH_ENABLED", auth_enabled)
    title = f"Cross origin {auth_enabled} {sorted(headers.items())}"
    # A text/plain body is what a no-cors fetch from another page sends.
    response = client.post("/api/projects", content=f'{{"title": "{title}"}}', headers={"Content-Type": "text/plain", **headers})
    assert response.status_code == 403
    assert title not in {project["title"] for project in store.list_projects()}
    assert client.post("/api/auth/login", json={"token": auth.TOKEN}, headers=headers).status_code == 403


def test_same_origin_changes_and_cross_origin_reads_pass(client):
    created = client.post(
        "/api/projects",
        json={"title": "Same origin"},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert created.status_code == 200, created.text
    # Reads are not checked: another origin cannot read the response anyway.
    assert client.get("/api/projects", headers={"Origin": "http://127.0.0.1:3000"}).status_code == 200


def test_websocket_refusals_arrive_as_close_codes(client):
    anonymous = TestClient(app)
    with anonymous.websocket_connect("/api/terminals/anything/ws") as socket_:
        with pytest.raises(WebSocketDisconnect) as info:
            socket_.receive_text()
    assert info.value.code == auth.CLOSE_UNAUTHORIZED

    with client.websocket_connect("/api/terminals/anything/ws", headers={**HEADERS, "Origin": "http://evil.example"}) as socket_:
        with pytest.raises(WebSocketDisconnect) as info:
            socket_.receive_text()
    assert info.value.code == auth.CLOSE_FORBIDDEN

    # TestClient ignores base_url for sockets; an absolute URL sets the Host header.
    with client.websocket_connect("ws://evil.example:8790/api/terminals/anything/ws", headers=HEADERS) as socket_:
        with pytest.raises(WebSocketDisconnect) as info:
            socket_.receive_text()
    assert info.value.code == auth.CLOSE_FORBIDDEN


# ------------------------------------------------------- cookie and launch code
def test_login_cookie_is_an_hmac_named_for_the_port():
    anonymous = TestClient(app)
    response = anonymous.post("/api/auth/login", json={"token": f"http://127.0.0.1:8790/?token={auth.TOKEN}"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    name, value = cookie.split(";", 1)[0].split("=", 1)
    assert name == "cryopilot_session_80"  # TestClient's server port
    assert value == auth.session_value() and value != auth.TOKEN
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie

    assert TestClient(app, headers={"Cookie": f"{name}={value}"}).get("/api/setup").status_code == 200
    # The raw token is no longer a valid cookie, and another port's cookie does not count.
    assert TestClient(app, headers={"Cookie": f"{name}={auth.TOKEN}"}).get("/api/setup").status_code == 401
    assert TestClient(app, headers={"Cookie": f"cryopilot_session_8791={value}"}).get("/api/setup").status_code == 401
    status = TestClient(app, headers={"Cookie": f"{name}={value}"}).get("/api/auth/status").json()
    assert status == {"enabled": True, "method": "token", "authenticated": True}


def test_launch_code_signs_in_once(monkeypatch):
    monkeypatch.setattr(auth, "LAUNCH_CODE", "launch-code-for-tests")
    monkeypatch.setattr(auth, "LAUNCH_DEADLINE", auth.time.monotonic() + 60)
    browser = TestClient(app, follow_redirects=False)
    first = browser.get("/?launch=launch-code-for-tests&view=home")
    assert first.status_code == 302
    assert first.headers["location"] == "/?view=home"
    assert first.headers["set-cookie"].startswith(f"cryopilot_session_80={auth.session_value()};")
    # Used once; a second visit (or another user replaying the URL) gets no cookie.
    second = browser.get("/?launch=launch-code-for-tests")
    assert second.status_code != 302 and "set-cookie" not in second.headers


def test_launch_code_expires(monkeypatch):
    monkeypatch.setattr(auth, "LAUNCH_CODE", "launch-code-for-tests")
    monkeypatch.setattr(auth, "LAUNCH_DEADLINE", auth.time.monotonic() - 1)
    response = TestClient(app, follow_redirects=False).get("/?launch=launch-code-for-tests")
    assert response.status_code != 302 and "set-cookie" not in response.headers


def test_wrong_tokens_pause_token_checks_but_not_signed_in_browsers(monkeypatch):
    monkeypatch.setattr(auth, "FAILURE_PAUSE", 60.0)
    anonymous = TestClient(app)
    # A browser without a cookie is the normal sign-in gate, not a failed guess.
    assert anonymous.get("/api/setup").status_code == 401
    assert anonymous.post("/api/auth/login", json={"token": auth.TOKEN}).status_code == 200

    assert anonymous.post("/api/auth/login", json={"token": "wrong"}).status_code == 401
    # Within the pause even the right token is not checked.
    assert anonymous.post("/api/auth/login", json={"token": auth.TOKEN}).status_code == 429
    assert TestClient(app, headers=HEADERS).get("/api/setup").status_code == 429
    assert TestClient(app, headers=HEADERS).get("/api/auth/status").json()["authenticated"] is False
    cookie = {"Cookie": f"cryopilot_session_80={auth.session_value()}"}
    assert TestClient(app, headers=cookie).get("/api/setup").status_code == 200

    monkeypatch.setattr(auth, "_paused_until", 0.0)
    assert TestClient(app, headers=HEADERS).get("/api/setup").status_code == 200


def test_no_auth_mode_skips_only_the_token(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    assert TestClient(app).get("/api/setup").status_code == 200
    assert TestClient(app).get("/api/auth/status").json() == {"enabled": False, "method": "token", "authenticated": True}


# ------------------------------------------------------------------ server
def test_transcript_images_are_inline_only_for_raster_types(client, monkeypatch):
    project = make_project("Images")
    session = store.create_session(project_id=project["id"], kind="reconstruction", title="Images", harness_session_id="h-images")
    stored = {"mime": "image/svg+xml", "data": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"}
    monkeypatch.setattr(main.trace_reader, "item_image", lambda *_: (stored["mime"], stored["data"]))
    monkeypatch.setattr(main.trace_reader, "recursive_item_image", lambda *_: (stored["mime"], stored["data"]))

    for url in (f"/api/sessions/{session['id']}/items/1/image/0", f"/api/sessions/{session['id']}/recursive/1/items/1/image/0"):
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == "attachment"
        assert "sandbox" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"

    stored.update(mime="image/png", data=b"\x89PNG\r\n\x1a\n")
    response = client.get(f"/api/sessions/{session['id']}/items/1/image/0")
    assert response.headers["content-type"] == "image/png"
    assert "content-disposition" not in response.headers
    assert response.headers["cache-control"] == "private, max-age=3600"


def test_only_hashed_bundles_are_cached_forever(client, tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "molstar").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "assets" / "index-3f2a1b.js").write_text("// bundle")
    (tmp_path / "molstar" / "molstar.js").write_text("// molstar")
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path)
    assert client.get("/assets/index-3f2a1b.js").headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/molstar/molstar.js").headers["cache-control"] == "no-cache"
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_reconstruction_effort_accepts_none(client):
    try:
        response = client.put("/api/settings", json={"defaults": {"reconstruction_effort": "none"}})
        assert response.status_code == 200
        assert defaults()["reconstruction_effort"] == "none"
        assert client.put("/api/settings", json={"defaults": {"modeling_effort": "none"}}).status_code == 422
    finally:
        client.put("/api/settings", json={"defaults": {"reconstruction_effort": ""}})


# ----------------------------------------------------------------- license
def test_license_is_served_and_accepting_it_is_remembered(client, monkeypatch):
    from app.server import routes_setup

    store.set_setting("license_accepted", None)
    assert client.get("/api/setup").json()["license"] == {
        "id": "PolyForm-Noncommercial-1.0.0",
        "name": "PolyForm Noncommercial License 1.0.0",
        "accepted": False,
        "accepted_at": None,
    }
    license = client.get("/api/license").json()
    assert license["accepted"] is False
    assert "# PolyForm Noncommercial License 1.0.0" in license["text"]
    assert "Required Notice: Copyright © 2026 Purdue University" in license["text"]

    accepted = client.post("/api/license/accept").json()["license"]
    assert accepted["accepted"] is True and accepted["accepted_at"]
    assert client.get("/api/setup").json()["license"]["accepted"] is True

    # A different license asks everyone again.
    monkeypatch.setattr(routes_setup, "LICENSE_ID", "Some-Other-License")
    assert client.get("/api/setup").json()["license"]["accepted"] is False
    store.set_setting("license_accepted", None)


def test_license_needs_the_access_check():
    anonymous = TestClient(app)
    assert anonymous.get("/api/license").status_code == 401
    assert anonymous.post("/api/license/accept").status_code == 401


# ------------------------------------------------------------------ resume
def _resume_fixture(monkeypatch, sandbox, *, provider: str | None):
    project = make_project(f"Resume {provider}")
    config = {"manifest": {}, "imported": True}
    if provider:
        config["agent"] = {"provider": provider}
    session = store.create_session(
        project_id=project["id"], kind="reconstruction", title="Imported", harness_session_id="h-not-in-db", config=config, status="stopped"
    )
    monkeypatch.setattr(main, "ALLOW_RECONSTRUCTION", True)
    monkeypatch.setattr(main, "cryosparc_settings", lambda: {"url": "http://localhost:61000", "email": "a@b.c", "password_set": True})
    key_checks = []
    monkeypatch.setattr(state, "require_openai_key", lambda: key_checks.append("openai"))
    monkeypatch.setattr(state, "anthropic_key", lambda: "")
    started = {}

    def failing_start(session_id, **kwargs):
        started.update(kwargs)
        raise OSError("python not found")

    monkeypatch.setattr(processes, "start_reconstruction", failing_start)
    return session, key_checks, started


def test_resume_uses_the_default_guide_and_reports_a_failed_start(client, monkeypatch, sandbox):
    store.set_setting("processing_guide", "experimental")
    try:
        session, key_checks, started = _resume_fixture(monkeypatch, sandbox, provider="ollama")
        events = bus.subscribe(session["id"])
        try:
            response = client.post(f"/api/sessions/{session['id']}/start")
        finally:
            bus.unsubscribe(session["id"], events)
    finally:
        store.set_setting("processing_guide", "original")
    assert response.status_code == 500
    # The imported run used the harness default, whatever Settings says now, and keeps it.
    assert started["env"]["CRYOEM_GUIDE"] == "original"
    row = store.get_session(session["id"])
    assert row["config"]["processing_guide"] == "original"
    # The session does not stay 'starting' after the driver failed to start.
    assert row["status"] == "error" and row["last_error"] == "python not found"
    published = []
    while True:
        try:
            published.append(events.get_nowait())
        except queue.Empty:
            break
    assert [event["state"] for event in published] == ["starting", "error"]
    assert published[-1]["running"] is False
    # An Ollama session needs no OpenAI key.
    assert key_checks == []


def test_resume_with_unknown_provider_requires_the_openai_key(client, monkeypatch, sandbox):
    session, key_checks, _ = _resume_fixture(monkeypatch, sandbox, provider=None)
    assert client.post(f"/api/sessions/{session['id']}/messages", json={"content": "Continue"}).status_code == 500
    assert key_checks == ["openai"]


def test_a_claude_modeling_session_requires_the_anthropic_key_only(client, monkeypatch, sandbox):
    project = make_project("Claude modeling")
    key_checks = []
    monkeypatch.setattr(state, "require_openai_key", lambda: key_checks.append("openai"))
    monkeypatch.setattr(state, "anthropic_key", lambda: "")
    body = {"prompt": "Validate the model", "model": "claude-sonnet-5-5"}
    response = client.post(f"/api/projects/{project['id']}/sessions/modeling", json=body)
    assert response.status_code == 400
    assert response.json()["setup"] == "anthropic"
    assert key_checks == []


def test_resume_of_a_claude_modeling_session_requires_the_anthropic_key(client, monkeypatch, sandbox):
    project = make_project("Claude modeling resume")
    session = store.create_session(
        project_id=project["id"], kind="modeling", title="Fit", config={"model": "claude-opus-5-5"}, status="stopped"
    )
    monkeypatch.setattr(state, "anthropic_key", lambda: "")
    response = client.post(f"/api/sessions/{session['id']}/start")
    assert response.status_code == 400
    assert response.json()["setup"] == "anthropic"


def test_resume_of_a_claude_session_requires_the_anthropic_key(client, monkeypatch, sandbox):
    session, key_checks, started = _resume_fixture(monkeypatch, sandbox, provider="anthropic")
    response = client.post(f"/api/sessions/{session['id']}/start")
    assert response.status_code == 400
    assert response.json()["setup"] == "anthropic"
    assert key_checks == [] and started == {}


def test_import_records_a_given_guide(client, monkeypatch):
    project = make_project("Import guide")
    monkeypatch.setattr(main.trace_reader, "session", lambda harness_id: {"manifest": {}, "project_uid": "P1", "workspace_uid": "W1"})
    imported = client.post(f"/api/projects/{project['id']}/sessions/reconstruction/import", json={"harness_session_id": "h-guide", "processing_guide": "experimental"})
    assert imported.status_code == 200, imported.text
    assert imported.json()["config"]["processing_guide"] == "experimental"
    refused = client.post(f"/api/projects/{project['id']}/sessions/reconstruction/import", json={"harness_session_id": "h-guide-2", "processing_guide": "v5"})
    assert refused.status_code == 422


# ------------------------------------------------------------ connection tests
@pytest.fixture
def openai_api(monkeypatch):
    """The real OpenAI SDK against a local stand-in for the API; set `status` and `body`."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            server.seen.append((self.path, self.headers.get("Authorization")))
            body = json.dumps(server.body).encode()
            self.send_response(server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.status, server.body, server.seen = 200, {"object": "list", "data": []}, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sdk_client = routes_setup.openai.OpenAI
    monkeypatch.setattr(routes_setup.openai, "OpenAI", lambda **kwargs: sdk_client(max_retries=0, **kwargs))
    monkeypatch.setattr(routes_setup, "openai_key", lambda: "sk-made-up")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    yield server
    server.shutdown()
    server.server_close()


def test_openai_test_lists_the_models(client, openai_api):
    default = defaults()["reconstruction_model"]
    names = ["z-model", default]
    openai_api.body = {
        "object": "list",
        "data": [{"id": name, "object": "model", "created": 1, "owned_by": "openai"} for name in names],
    }
    response = client.post("/api/settings/openai/test")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "model_count": 2,
        "default_model_available": True,
        "models": sorted(names),
    }
    assert openai_api.seen == [("/v1/models", "Bearer sk-made-up")]


@pytest.mark.parametrize(
    "status, message",
    [(401, "OpenAI rejected this API key."), (429, "OpenAI returned HTTP 429."), (500, "OpenAI returned HTTP 500.")],
)
def test_openai_test_reports_what_the_api_answered(client, openai_api, status, message):
    openai_api.status = status
    openai_api.body = {"error": {"message": "no"}}
    response = client.post("/api/settings/openai/test")
    assert response.status_code == 502
    assert response.json()["message"] == message


def test_openai_test_says_why_it_could_not_connect(client, openai_api, monkeypatch):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))  # Bound but not listening, so a connection is refused.
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{held.getsockname()[1]}/v1")
    response = client.post("/api/settings/openai/test")
    held.close()
    assert response.status_code == 502
    message = response.json()["message"]
    assert message.startswith("Could not reach OpenAI: ")
    assert message != "Could not reach OpenAI: Connection error."


def test_openai_test_needs_a_key(client, monkeypatch):
    monkeypatch.setattr(routes_setup, "openai_key", lambda: "")
    response = client.post("/api/settings/openai/test")
    assert response.status_code == 400
    assert response.json()["message"] == "Enter an OpenAI API key first."


# ---------------------------------------------------------- agent environments
def test_each_agent_gets_only_its_own_account():
    modeling = driver_env("modeling")
    assert "OPENAI_API_KEY" in modeling and "CRYOZETA_PASSWORD" in modeling
    assert not any(name.startswith("CRYOSPARC_") for name in modeling)
    assert "ANTHROPIC_API_KEY" in modeling
    reconstruction = driver_env("reconstruction", guide="experimental")
    assert reconstruction["CRYOEM_GUIDE"] == "experimental" and "CRYOSPARC_PASSWORD" in reconstruction
    assert "ANTHROPIC_API_KEY" in reconstruction
    assert not any(name.startswith("CRYOZETA_") for name in reconstruction)


def test_cleared_secrets_do_not_come_back_from_the_server_environment(monkeypatch):
    monkeypatch.setenv("CRYOZETA_PASSWORD", "exported-before-start")
    monkeypatch.setenv("CRYOSPARC_PASSWORD", "exported-before-start")
    monkeypatch.setenv("CRYOPILOT_TOKEN", "not-for-agents")
    env = processes._base_env({"OPENAI_API_KEY": "sk-test", "CRYOZETA_USERNAME": "user", "CRYOZETA_PASSWORD": ""})
    assert env["OPENAI_API_KEY"] == "sk-test" and env["CRYOZETA_USERNAME"] == "user"
    for name in ("CRYOZETA_PASSWORD", "CRYOSPARC_PASSWORD", "CRYOPILOT_TOKEN"):
        assert name not in env
    assert set(processes_module.MANAGED_ENV) >= {"OPENAI_API_KEY", "CRYOSPARC_EMAIL", "CRYOZETA_BASE_URL"}


def test_launch_code_and_cookie_secret_leave_the_environment():
    assert "CRYOPILOT_LAUNCH_CODE" not in os.environ
    assert "CRYOPILOT_COOKIE_SECRET" not in os.environ


# --------------------------------------------------------------- credentials
def test_credentials_file_is_private_and_never_written_through_a_planted_link(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "CRYOSPARC_PASSWORD", "CRYOZETA_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    victim = tmp_path / "victim"
    victim.write_text("")
    (tmp_path / "credentials.tmp").symlink_to(victim)
    credentials = Credentials(tmp_path / "credentials.json")
    credentials.set_remember(True)
    credentials.set("openai_api_key", "sk-secret")

    assert victim.read_text() == ""
    saved = tmp_path / "credentials.json"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert "sk-secret" in saved.read_text()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["credentials.json", "credentials.tmp", "victim"]
    assert Credentials(saved).get("openai_api_key") == "sk-secret"


def test_app_home_is_private():
    # A folder an older version (or a permissive umask) left open is closed at startup.
    config.APP_HOME.chmod(0o755)
    config.ensure_directories()
    for path in (config.APP_HOME, config.PROJECTS_DIR):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


# -------------------------------------------------------------------- serve.py
def test_port_check_sees_a_held_port():
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        assert serve.port_is_free("127.0.0.1", port) is False
    assert serve.port_is_free("127.0.0.1", port) is True


def test_short_tokens_are_refused(monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("CRYOPILOT_TOKEN", "short")
    monkeypatch.setenv("CRYOPILOT_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(serve, "check_python_dependencies", lambda: None)
    assert serve.main(["--no-open", "--no-build"]) == 2
    assert "at least 22 characters" in capsys.readouterr().err


# ---------------------------------------------------------- experimental features
def test_experimental_setting_round_trip(client):
    try:
        assert client.get("/api/setup").json()["experimental"] == {"enabled": False}
        saved = client.put("/api/settings", json={"experimental": {"enabled": True}}).json()
        assert saved["experimental"] == {"enabled": True}
        assert client.get("/api/setup").json()["experimental"]["enabled"] is True
    finally:
        store.set_setting("experimental", {})


def test_web_tools_flag_reaches_only_the_reconstruction_agent(monkeypatch):
    # A value exported where the server started cannot turn the tools on.
    monkeypatch.setenv("CRYOPILOT_WEB_TOOLS", "1")
    assert processes._base_env(driver_env("reconstruction"))["CRYOPILOT_WEB_TOOLS"] == "0"
    assert driver_env("reconstruction", web_tools=True)["CRYOPILOT_WEB_TOOLS"] == "1"
    assert "CRYOPILOT_WEB_TOOLS" not in driver_env("modeling", web_tools=True)


def test_a_session_resumes_with_the_experimental_setting_it_started_with(client, monkeypatch, sandbox):
    session, _, started = _resume_fixture(monkeypatch, sandbox, provider="ollama")
    store.update_session(session["id"], config={**session["config"], "experimental": True})
    store.set_setting("experimental", {"enabled": False})
    client.post(f"/api/sessions/{session['id']}/start")
    assert started["env"]["CRYOPILOT_WEB_TOOLS"] == "1"

    other, _, started_other = _resume_fixture(monkeypatch, sandbox, provider="ollama")
    store.set_setting("experimental", {"enabled": True})
    try:
        client.post(f"/api/sessions/{other['id']}/start")
    finally:
        store.set_setting("experimental", {})
    # Sessions from before the setting, or started with it off, stay without web tools.
    assert started_other["env"]["CRYOPILOT_WEB_TOOLS"] == "0"


def test_import_records_the_experimental_setting(client, monkeypatch):
    project = make_project("Import experimental")
    monkeypatch.setattr(main.trace_reader, "session", lambda harness_id: {"manifest": {}, "project_uid": "P1", "workspace_uid": "W1"})
    store.set_setting("experimental", {"enabled": True})
    try:
        imported = client.post(f"/api/projects/{project['id']}/sessions/reconstruction/import", json={"harness_session_id": "h-exp"})
    finally:
        store.set_setting("experimental", {})
    assert imported.status_code == 200, imported.text
    assert imported.json()["config"]["experimental"] is True
