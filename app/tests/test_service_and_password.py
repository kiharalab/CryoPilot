"""Tests for the shared password and the background-server commands.

Run from the repository root:
    .venv/bin/python -m pytest app/tests/test_service_and_password.py -q
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.server import config  # noqa: E402

if config.APP_HOME == Path("~/.cryopilot/app").expanduser():
    pytest.skip("the app home is the user's real one; run with CRYOPILOT_APP_HOME set", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import serve, service  # noqa: E402
from app.server import auth  # noqa: E402
from app.server import password as lab_password  # noqa: E402
from app.server.main import app  # noqa: E402

PASSWORD = "lab password 1"


@pytest.fixture(autouse=True)
def private_files(monkeypatch, tmp_path):
    """Password, cookie secret, and service files in a temporary folder; token sign-in by default."""

    monkeypatch.setattr(lab_password, "PASSWORD_PATH", tmp_path / "password.json")
    monkeypatch.setattr(lab_password, "COOKIE_SECRET_PATH", tmp_path / "cookie_secret")
    for name in ("SETTINGS_PATH", "PID_PATH", "LOG_PATH"):
        monkeypatch.setattr(service, name, tmp_path / getattr(service, name).name)
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "PASSWORD", None)
    monkeypatch.setattr(auth, "_paused_until", 0.0)
    monkeypatch.setattr(auth, "_launch_code_used", False)
    return tmp_path


@pytest.fixture
def password_mode(monkeypatch):
    lab_password.save(PASSWORD)
    monkeypatch.setattr(auth, "PASSWORD", lab_password.load())


# ------------------------------------------------------------------ password
def test_password_file_holds_only_a_private_hash(private_files):
    lab_password.save(PASSWORD)
    path = private_files / "password.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert PASSWORD not in path.read_text()
    record = lab_password.load()
    assert lab_password.verify(record, PASSWORD)
    assert not lab_password.verify(record, PASSWORD + " ")
    assert not lab_password.verify(record, "")


def test_short_passwords_are_refused():
    with pytest.raises(ValueError):
        lab_password.save("short")
    assert not lab_password.is_set()


def test_cookie_secret_is_kept_on_disk(private_files):
    first = lab_password.cookie_secret()
    assert lab_password.cookie_secret() == first
    assert stat.S_IMODE((private_files / "cookie_secret").stat().st_mode) == 0o600


def test_password_replaces_the_token(password_mode):
    anonymous = TestClient(app)
    assert anonymous.get("/api/auth/status").json() == {"enabled": True, "method": "password", "authenticated": False}
    # Neither the token nor a header gets in once a password is set.
    assert anonymous.post("/api/auth/login", json={"token": auth.TOKEN}).status_code == 401
    by_header = TestClient(app, headers={"Authorization": f"Bearer {auth.TOKEN}"})
    assert by_header.get("/api/setup").status_code == 401
    link = TestClient(app, follow_redirects=False).get(f"/?token={auth.TOKEN}")
    assert link.status_code != 302 and "set-cookie" not in link.headers

    # The wrong token above paused checks for a second.
    auth._paused_until = 0.0
    wrong = anonymous.post("/api/auth/login", json={"password": "not it at all"})
    assert wrong.status_code == 401 and wrong.json()["error"] == "That password is not right."
    auth._paused_until = 0.0
    right = anonymous.post("/api/auth/login", json={"password": PASSWORD})
    assert right.status_code == 200
    value = right.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    assert value == auth.session_value()
    signed_in = TestClient(app, headers={"Cookie": f"cryopilot_session_80={value}"})
    assert signed_in.get("/api/setup").status_code == 200


def test_a_new_password_signs_everyone_out(password_mode, monkeypatch):
    old_cookie = auth.session_value()
    lab_password.save("another lab password")
    monkeypatch.setattr(auth, "PASSWORD", lab_password.load())
    assert auth.session_value() != old_cookie
    assert TestClient(app, headers={"Cookie": f"cryopilot_session_80={old_cookie}"}).get("/api/setup").status_code == 401


def test_unauthorized_requests_name_the_password(password_mode):
    response = TestClient(app).get("/api/setup")
    assert response.status_code == 401 and "password" in response.json()["error"]


# ------------------------------------------------------------------ serve.py
def test_no_auth_is_refused_beyond_localhost(monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setattr(serve, "check_python_dependencies", lambda: None)
    assert serve.main(["--no-auth", "--host", "0.0.0.0", "--no-open", "--no-build"]) == 2
    assert "cryopilot password" in capsys.readouterr().err


# ------------------------------------------------------------------- service
def test_start_without_a_password_asks_for_one(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert service.main(["start", "--lan"]) == 2
    assert "cryopilot password" in capsys.readouterr().err
    assert not service.PID_PATH.exists()


def test_password_command_reads_a_line_from_a_script(monkeypatch, tmp_path, capsys):
    source = tmp_path / "input.txt"
    source.write_text(PASSWORD + "\n")
    monkeypatch.setattr(sys, "stdin", open(source))
    assert service.main(["password"]) == 0
    assert lab_password.verify(lab_password.load(), PASSWORD)
    assert "Saved" in capsys.readouterr().out


def test_status_when_stopped_and_stale_records_are_removed(capsys):
    assert service.main(["status"]) == 3
    # A record whose process is gone, from this computer.
    service.PID_PATH.write_text(json.dumps({"pid": 2**22 + 12345, "machine": service._this_machine(), "host": "0.0.0.0", "port": 8790, "started": "2026-10-23T12:00:00+00:00"}))
    assert service.server_record() is None
    assert not service.PID_PATH.exists()
    capsys.readouterr()


def test_a_server_on_another_computer_is_left_alone(capsys):
    record = {"pid": os.getpid(), "machine": "dove", "host": "0.0.0.0", "port": 8790, "started": "2026-10-23T12:00:00+00:00"}
    service.PID_PATH.write_text(json.dumps(record))
    assert service.main(["stop"]) == 1
    assert "dove" in capsys.readouterr().err
    assert service.PID_PATH.exists()
    lab_password.save(PASSWORD)
    assert service.main(["start"]) == 1


def test_lan_addresses_skip_loopback():
    links = service.addresses("0.0.0.0", 8790)
    assert links and all(link.endswith(":8790") for link in links)
    assert not any("127.0.0.1" in link or "0.0.0.0" in link for link in links)
    assert service.addresses("127.0.0.1", 8790) == ["http://localhost:8790"]
