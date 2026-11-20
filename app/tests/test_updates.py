"""Tests for finding, installing, and cleaning up after CryoPilot updates.

Run from the repository root:
    .venv/bin/python -m pytest app/tests/test_updates.py -q
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.server import config  # noqa: E402

if config.APP_HOME == Path("~/.cryopilot/app").expanduser():
    pytest.skip("the app home is the user's real one; run with CRYOPILOT_APP_HOME set", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import service  # noqa: E402
from app.server import auth, updates  # noqa: E402
from app.server.main import app  # noqa: E402
from app.server.state import processes  # noqa: E402

BUILD = "abcdef123456"


def program_text(version: str, exit_code: int = 0) -> bytes:
    return f'#!/bin/sh\necho "cryopilot {version}"\nexit {exit_code}\n'.encode()


@pytest.fixture
def site(tmp_path):
    """A web server for releases.json and the download."""

    root = tmp_path / "site"
    root.mkdir()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def publish(site, version="9.9.9", content=None, sha256=None):
    root, address = site
    content = program_text(version) if content is None else content
    (root / "program").write_bytes(content)
    latest = {
        "version": version,
        "date": "2026-11-04",
        "url": f"{address}/program",
        "sha256": sha256 or hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "unpacked_mb": 1,
    }
    manifest = {"latest": latest, "releases": [{"version": version, "date": "2026-11-04", "notes": ["A new thing."]}, {"version": updates.VERSION, "date": "2026-11-03", "notes": ["An old thing."]}]}
    (root / "releases.json").write_text(json.dumps(manifest), encoding="utf-8")
    return latest


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """A CryoPilot installed in its own folder, unpacked in a cache folder, with its data."""

    monkeypatch.setattr(updates, "CACHE_PATH", tmp_path / "home" / "releases.json")
    (tmp_path / "home").mkdir()
    folder = tmp_path / "share"
    folder.mkdir()
    program = folder / updates.PROGRAM_NAME
    program.write_bytes(program_text(updates.VERSION))
    program.chmod(0o755)
    monkeypatch.setenv("CRYOPILOT_BINARY", str(program))
    own = tmp_path / "cache" / f"{updates.VERSION}-{BUILD}"
    own.mkdir(parents=True)
    (own / ".complete").touch()
    monkeypatch.setattr(updates, "own_folder", lambda: own)
    monkeypatch.setattr(updates, "_used_here", lambda folder: False)
    return program


def contents(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def unpacked(cache: Path, version: str, *files: str, age: float = 0, build: str = BUILD) -> Path:
    folder = cache / f"{version}-{build}"
    folder.mkdir()
    (folder / "cryopilot").write_text("program")
    for name in (".complete", *files):
        path = folder / name
        path.touch()
        if age:
            os.utime(path, (time.time() - age, time.time() - age))
    return folder


# ------------------------------------------------------------------ checking
def test_only_a_newer_version_counts_as_an_update(monkeypatch):
    monkeypatch.setattr(updates, "VERSION", "1.10.0")
    latest = lambda version: {"latest": {"version": version}, "releases": []}  # noqa: E731
    assert updates.newer_release(latest("1.9.9")) is None
    assert updates.newer_release(latest("1.10.0")) is None
    assert updates.newer_release(latest("1.10.1"))["version"] == "1.10.1"
    assert updates.newer_release(None) is None


def test_the_check_keeps_what_it_read_and_stays_quiet_when_the_site_is_down(site, installed, monkeypatch):
    publish(site)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "0")
    updates.check_daily()
    assert updates.newer_release(updates.cached_manifest())["version"] == "9.9.9"
    assert "9.9.9" in updates.notice()

    monkeypatch.setattr(updates, "RELEASES_URL", "http://127.0.0.1:9/releases.json")
    updates.CACHE_PATH.unlink()
    updates.check_daily()
    assert updates.cached_manifest() is None
    assert updates.notice() is None


def test_the_check_runs_once_a_day(site, installed, monkeypatch):
    publish(site)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "0")
    updates.check_daily()
    (site[0] / "releases.json").write_text("not read again", encoding="utf-8")
    updates.check_daily()
    assert updates.newer_release(updates.cached_manifest())["version"] == "9.9.9"


def test_a_server_start_asks_again_within_the_day(site, installed, monkeypatch):
    publish(site, version="9.9.8")
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "0")
    updates.check_daily()
    publish(site, version="9.9.9")
    updates.check_daily()
    assert updates.newer_release(updates.cached_manifest())["version"] == "9.9.8"
    updates.check_daily(force=True)
    assert updates.newer_release(updates.cached_manifest())["version"] == "9.9.9"


def test_the_background_check_asks_at_start_and_prints_the_notice(site, installed, monkeypatch, capsys):
    publish(site, version="9.9.8")
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "0")
    updates.check_daily()
    publish(site, version="9.9.9")
    monkeypatch.setattr(updates, "remove_old_versions", lambda: None)
    monkeypatch.setattr(updates, "mark_in_use", lambda: None)

    class Stop(Exception):
        pass

    def stop(_seconds):
        raise Stop

    monkeypatch.setattr(updates.time, "sleep", stop)
    with pytest.raises(Stop):
        updates._background()
    assert "CryoPilot 9.9.9 is available" in capsys.readouterr().out


def test_turning_the_check_off_sends_nothing_and_shows_nothing(site, installed, monkeypatch):
    publish(site)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "1")
    updates.check_daily()
    assert not updates.CACHE_PATH.exists()
    assert updates.notice() is None


@pytest.mark.parametrize("text", ["not json", "[]", '{"latest": {"version": "../../x"}, "releases": []}', '{"latest": {"version": "9.9.9"}}'])
def test_a_bad_list_of_releases_is_refused(site, installed, monkeypatch, text):
    (site[0] / "releases.json").write_text(text, encoding="utf-8")
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    with pytest.raises(updates.UpdateError):
        updates.fetch_manifest()
    assert not updates.CACHE_PATH.exists()


# ---------------------------------------------------------------- installing
def test_an_update_replaces_the_program_and_leaves_nothing_else(site, installed):
    data = installed.parent.parent / "home" / "app.sqlite3"
    data.write_text("projects")
    new = updates.install(publish(site))
    assert new == installed
    assert subprocess.run([str(new), "--version"], capture_output=True, text=True).stdout.strip() == "cryopilot 9.9.9"
    assert contents(installed.parent) == [updates.PROGRAM_NAME]
    assert stat.S_IMODE(new.stat().st_mode) == 0o755
    assert data.read_text() == "projects"


def test_a_hand_downloaded_copy_gets_the_new_versions_name(site, installed, monkeypatch):
    copy = installed.with_name(f"cryopilot-{updates.VERSION}-linux-x86_64")
    installed.rename(copy)
    monkeypatch.setenv("CRYOPILOT_BINARY", str(copy))
    new = updates.install(publish(site))
    assert new.name == "cryopilot-9.9.9-linux-x86_64"
    assert contents(copy.parent) == [new.name]


def test_a_damaged_download_changes_nothing(site, installed):
    before = installed.read_bytes()
    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.install(publish(site, sha256="0" * 64))
    assert installed.read_bytes() == before
    assert contents(installed.parent) == [updates.PROGRAM_NAME]


@pytest.mark.parametrize(
    "content",
    [program_text("9.9.9", exit_code=1).replace(b"echo", b"false && echo"), program_text("1.2.3"), b"not a program"],
    ids=["fails", "wrong version", "not a program"],
)
def test_a_new_version_that_does_not_start_changes_nothing(site, installed, content):
    before = installed.read_bytes()
    with pytest.raises(updates.UpdateError, match="did not start"):
        updates.install(publish(site, content=content))
    assert installed.read_bytes() == before
    assert contents(installed.parent) == [updates.PROGRAM_NAME]


def test_a_failed_check_removes_only_what_it_unpacked(site, installed, monkeypatch):
    cache = updates.own_folder().parent
    earlier = unpacked(cache, "9.9.9", build="111111111111")
    release = publish(site, content=program_text("1.2.3"))
    real_check = updates._check_it_starts

    def unpack_then_check(partial, version):
        unpacked(cache, version, build="222222222222")
        real_check(partial, version)

    monkeypatch.setattr(updates, "_check_it_starts", unpack_then_check)
    with pytest.raises(updates.UpdateError, match="did not start"):
        updates.install(release)
    assert earlier.exists()
    assert not (cache / "9.9.9-222222222222").exists()


def test_a_download_that_cannot_be_reached_changes_nothing(site, installed):
    release = {**publish(site), "url": "http://127.0.0.1:9/program"}
    with pytest.raises(updates.UpdateError, match="download failed"):
        updates.install(release)
    assert contents(installed.parent) == [updates.PROGRAM_NAME]


def test_too_little_disk_space_stops_before_the_download(site, installed, monkeypatch):
    release = {**publish(site), "size": 10 ** 15}
    with pytest.raises(updates.UpdateError, match="free"):
        updates.install(release)
    assert contents(installed.parent) == [updates.PROGRAM_NAME]


@pytest.mark.parametrize(
    "change",
    [{"url": "http://example.com/program"}, {"url": "ftp://127.0.0.1/program"}, {"sha256": "short"}, {"version": "../x"}, {"size": None}],
    ids=["plain http", "ftp", "short checksum", "strange version", "no size"],
)
def test_an_incomplete_release_is_refused(site, installed, change):
    with pytest.raises(updates.UpdateError, match="not complete"):
        updates.install({**publish(site), **change})
    assert contents(installed.parent) == [updates.PROGRAM_NAME]


def test_two_updates_cannot_run_at_once_but_a_killed_one_does_not_block(site, installed):
    partial = installed.with_name(updates.PARTIAL_NAME)
    partial.touch()
    release = publish(site)
    with pytest.raises(updates.UpdateError, match="Another update"):
        updates.install(release)
    assert partial.exists()
    os.utime(partial, (time.time() - updates.PARTIAL_STALE - 1, time.time() - updates.PARTIAL_STALE - 1))
    updates.install(release)
    assert contents(installed.parent) == [updates.PROGRAM_NAME]


def test_a_copy_from_source_or_a_read_only_folder_explains_itself(installed, monkeypatch):
    monkeypatch.setattr(updates.os, "access", lambda path, mode: False)
    assert "cannot write" in updates.install_blocker()
    monkeypatch.delenv("CRYOPILOT_BINARY")
    assert "from source" in updates.install_blocker()


def test_an_update_counts_like_the_installer_and_can_opt_out(monkeypatch):
    sent = []
    monkeypatch.setattr(updates, "_open", lambda url, timeout, data=None: sent.append((url, data)) or open(os.devnull))
    monkeypatch.setenv("CRYOPILOT_NO_ANALYTICS", "1")
    updates.count_install("9.9.9")
    assert sent == []
    monkeypatch.setenv("CRYOPILOT_NO_ANALYTICS", "0")
    updates.count_install("9.9.9")
    assert sent == [(updates.INSTALL_COUNT_URL, b"version=9.9.9&kind=upgrade")]
    monkeypatch.setattr(updates, "_open", lambda url, timeout, data=None: (_ for _ in ()).throw(OSError("offline")))
    updates.count_install("9.9.9")


# ------------------------------------------------------- what old versions left
def test_old_versions_go_and_everything_else_stays(installed, monkeypatch):
    own = updates.own_folder()
    cache = own.parent
    old = unpacked(cache, "0.0.1", age=updates.UNMARKED_KEEP + 60)
    same_version_other_build = unpacked(cache, updates.VERSION, age=updates.UNMARKED_KEEP + 60, build="111111111111")
    newer = unpacked(cache, "99.0.0", age=updates.UNMARKED_KEEP + 60)
    interrupted = cache / f"0.0.2-{BUILD}"
    interrupted.mkdir()
    (cache / "notes").mkdir()
    (cache / "0.0.3-notabuild").mkdir()
    (cache / ".lock").touch()
    link = cache / f"0.0.4-{BUILD}"
    link.symlink_to(cache / "notes")

    assert sorted(updates.remove_old_versions()) == ["0.0.1", "0.0.2", updates.VERSION]
    assert not old.exists() and not same_version_other_build.exists() and not interrupted.exists()
    assert own.exists() and newer.exists()
    assert (cache / "notes").is_dir() and (cache / "0.0.3-notabuild").is_dir() and (cache / ".lock").exists() and link.is_symlink()


def test_a_folder_a_process_here_runs_from_stays(installed, monkeypatch):
    cache = updates.own_folder().parent
    old = unpacked(cache, "0.0.1", age=updates.UNMARKED_KEEP + 60)
    monkeypatch.setattr(updates, "_used_here", lambda folder: folder == old)
    assert updates.remove_old_versions() == []
    assert old.exists()


def test_a_recent_mark_from_another_computer_keeps_the_folder(installed, monkeypatch):
    cache = updates.own_folder().parent
    monkeypatch.setattr(updates.socket, "gethostname", lambda: "here")
    used_elsewhere = unpacked(cache, "0.0.1", ".used-on-there", build="111111111111")
    stale_elsewhere = unpacked(cache, "0.0.2", ".used-on-there", age=updates.MARK_STALE + 60)
    only_here = unpacked(cache, "0.0.3", ".used-on-here")
    assert sorted(updates.remove_old_versions()) == ["0.0.2", "0.0.3"]
    assert used_elsewhere.exists() and not stale_elsewhere.exists() and not only_here.exists()


def test_a_folder_without_marks_waits_a_week(installed):
    cache = updates.own_folder().parent
    recent = unpacked(cache, "0.0.1", age=3600)
    assert updates.remove_old_versions() == []
    assert recent.exists()


def test_a_start_killed_while_unpacking_is_cleared_after_a_day(installed):
    cache = updates.own_folder().parent
    fresh = cache / ".unpack-1.0.0-abc.XYZ"
    old = cache / ".unpack-1.0.0-abc.OLD"
    fresh.mkdir()
    old.mkdir()
    os.utime(old, (time.time() - updates.UNPACK_STALE - 60, time.time() - updates.UNPACK_STALE - 60))
    updates.remove_old_versions()
    assert fresh.exists() and not old.exists()


def test_a_copy_from_source_removes_nothing(monkeypatch):
    monkeypatch.setattr(updates, "own_folder", lambda: None)
    assert updates.remove_old_versions() == []


@pytest.mark.skipif(not Path("/proc/self/exe").exists(), reason="needs /proc")
def test_processes_are_found_in_proc(tmp_path):
    real = Path(sys.executable).resolve().parent
    assert updates.__dict__["_used_here"](real)
    assert not updates.__dict__["_used_here"](tmp_path)


def test_each_server_marks_its_folder(installed, monkeypatch):
    monkeypatch.setattr(updates.socket, "gethostname", lambda: "here")
    updates.mark_in_use()
    assert (updates.own_folder() / ".used-on-here").exists()


# ------------------------------------------------------------- the command line
def run_update(monkeypatch, *flags):
    import argparse

    args = argparse.Namespace(check="--check" in flags, yes="--yes" in flags)
    return service.command_update(args)


def test_the_command_says_when_nothing_is_newer(site, installed, monkeypatch, capsys):
    publish(site, version=updates.VERSION)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    assert run_update(monkeypatch) == 0
    assert "latest version" in capsys.readouterr().out


def test_check_shows_what_changed_and_installs_nothing(site, installed, monkeypatch, capsys):
    before = installed.read_bytes()
    publish(site)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    assert run_update(monkeypatch, "--check") == 0
    output = capsys.readouterr().out
    assert "9.9.9" in output and "A new thing." in output and "An old thing." not in output
    assert installed.read_bytes() == before


def test_the_command_does_not_install_unasked_without_a_terminal(site, installed, monkeypatch, capsys):
    before = installed.read_bytes()
    publish(site)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert run_update(monkeypatch) == 2
    assert installed.read_bytes() == before


def test_the_command_installs_and_hands_over_to_the_new_program(site, installed, monkeypatch, capsys):
    publish(site)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    started = []
    monkeypatch.setattr(service.os, "execv", lambda path, argv: started.append(argv))
    run_update(monkeypatch, "--yes")
    assert started == [[str(installed), updates.REMOVE_OLD_ARGUMENT]]
    assert b"9.9.9" in installed.read_bytes()


def test_the_command_reports_a_site_it_cannot_reach(installed, monkeypatch, capsys):
    monkeypatch.setattr(updates, "RELEASES_URL", "http://127.0.0.1:9/releases.json")
    assert run_update(monkeypatch) == 1
    assert "Could not read" in capsys.readouterr().err


# ------------------------------------------------------------------- the app
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False, raising=False)
    monkeypatch.setenv("CRYOPILOT_AUTH", "0")
    monkeypatch.setattr(updates, "_job", {"state": "idle", "version": None, "done": 0, "total": 0, "error": None})
    monkeypatch.setattr(updates, "_new_program", None)
    monkeypatch.setattr(updates, "restart_requested", False)
    monkeypatch.setattr(updates, "stop_server", None)
    return TestClient(app)


def test_the_check_button_reads_the_site_at_once(site, installed, monkeypatch, client):
    publish(site, version="9.9.8")
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "0")
    updates.check_daily()
    first = client.get("/api/update").json()
    assert first["latest"] == "9.9.8" and first["checks_off"] is False
    assert first["checked_at"] == pytest.approx(time.time(), abs=60)

    publish(site, version="9.9.9")
    view = client.post("/api/update/check")
    assert view.status_code == 200
    assert view.json()["latest"] == "9.9.9"

    monkeypatch.setattr(updates, "RELEASES_URL", "http://127.0.0.1:9/releases.json")
    failed = client.post("/api/update/check")
    assert failed.status_code == 502
    assert "Could not read" in failed.json()["message"]
    assert client.get("/api/update").json()["latest"] == "9.9.9"


def test_the_check_button_sends_nothing_when_checks_are_off(site, installed, monkeypatch, client):
    publish(site)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "1")
    answer = client.post("/api/update/check")
    assert answer.status_code == 409
    assert not updates.CACHE_PATH.exists()
    view = client.get("/api/update").json()
    assert view["checks_off"] is True and view["checked_at"] is None


def wait_for_install(client):
    for _ in range(200):
        state = client.get("/api/update").json()["install"]
        if state["state"] in {"ready", "failed"}:
            return state
        time.sleep(0.05)
    raise AssertionError("the install did not finish")


def test_the_app_finds_installs_and_restarts(site, installed, monkeypatch, client):
    publish(site)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "0")
    updates.fetch_manifest()

    view = client.get("/api/update").json()
    assert view["latest"] == "9.9.9" and view["blocker"] is None
    assert [entry["version"] for entry in view["new_releases"]] == ["9.9.9"]
    assert client.get("/api/setup").json()["version"] == updates.VERSION

    assert client.post("/api/update/restart").status_code == 409
    assert client.post("/api/update/install").status_code == 200
    assert wait_for_install(client)["state"] == "ready"

    stopped = threading.Event()
    monkeypatch.setattr(updates, "stop_server", stopped.set)
    monkeypatch.setattr(processes, "running", lambda: 2)
    answer = client.post("/api/update/restart")
    assert answer.status_code == 409 and "2 agents still running" in answer.json()["error"]
    assert not updates.restart_requested

    monkeypatch.setattr(processes, "running", lambda: 0)
    assert client.post("/api/update/restart").status_code == 200
    assert stopped.wait(5) and updates.restart_requested and updates.new_program() == installed


def test_the_app_shows_why_an_update_failed(site, installed, monkeypatch, client):
    publish(site, sha256="0" * 64)
    monkeypatch.setattr(updates, "RELEASES_URL", f"{site[1]}/releases.json")
    monkeypatch.setenv("CRYOPILOT_NO_UPDATE_CHECK", "0")
    updates.fetch_manifest()
    assert client.post("/api/update/install").status_code == 200
    state = wait_for_install(client)
    assert state["state"] == "failed" and "checksum" in state["error"]
    assert contents(installed.parent) == [updates.PROGRAM_NAME]


def test_the_app_will_not_install_when_nothing_is_newer(installed, client):
    assert client.post("/api/update/install").status_code == 409


# ------------------------------------------------------------------ packaging
# The build scripts are not in every copy of this repository, and the launcher
# and the installer are for Linux (GNU mv, readlink -f, uname).
needs_build_scripts = pytest.mark.skipif(not (REPO_ROOT / "packaging").is_dir(), reason="needs packaging/")
only_on_linux = pytest.mark.skipif(sys.platform != "linux" or not (REPO_ROOT / "packaging").is_dir(), reason="needs Linux and packaging/")


def packed(tmp_path, version="9.9.9") -> Path:
    """A real single-file program, made by packaging/pack_single_file.sh."""

    dist = tmp_path / "dist"
    dist.mkdir()
    program = dist / "cryopilot"
    program.write_text(f'#!/bin/sh\necho "cryopilot {version} from $CRYOPILOT_BINARY"\n')
    program.chmod(0o755)
    (dist / "padding").write_bytes(os.urandom(4096))
    output = tmp_path / f"cryopilot-{version}-linux-x86_64"
    subprocess.run([str(REPO_ROOT / "packaging/pack_single_file.sh"), str(dist), str(output), version], check=True, capture_output=True)
    return output


def run_packed(path: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CRYOPILOT_CACHE": str(tmp_path / "cache")}
    return subprocess.run([str(path)], capture_output=True, text=True, env=environment)


@only_on_linux
def test_the_launcher_tells_the_program_where_it_is(tmp_path):
    program = packed(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(program)
    assert run_packed(link, tmp_path).stdout.strip() == f"cryopilot 9.9.9 from {program.resolve()}"


@only_on_linux
def test_a_cut_off_file_fails_even_when_its_version_is_already_unpacked(tmp_path):
    program = packed(tmp_path)
    assert run_packed(program, tmp_path).returncode == 0
    cut = tmp_path / "cut"
    cut.write_bytes(program.read_bytes()[:-100])
    cut.chmod(0o755)
    finished = run_packed(cut, tmp_path)
    assert finished.returncode == 2 and "incomplete" in finished.stderr


def run_installer(tmp_path: Path, home: Path) -> str:
    """install.sh against a local copy of the download folder, without a terminal."""

    site = tmp_path / "downloads"
    version = re.search(r"^LATEST=(.+)$", (REPO_ROOT / "install.sh").read_text(), re.MULTILINE)[1]
    binary = packed(tmp_path / "build", version) if (tmp_path / "build").mkdir() is None else None
    folder = site / f"v{version.replace('.', '_')}"
    folder.mkdir(parents=True)
    (folder / binary.name).write_bytes(binary.read_bytes())
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    (folder / f"{binary.name}.sha256").write_text(f"{digest}  {binary.name}\n")
    script = (REPO_ROOT / "install.sh").read_text().replace("DOWNLOADS=https://kiharalab.org/CryoPilot/app", f"DOWNLOADS=file://{site}")
    environment = {"PATH": os.environ["PATH"], "HOME": str(home), "SHELL": "/bin/bash", "CRYOPILOT_NO_ANALYTICS": "1"}
    finished = subprocess.run(["sh", "-s", "--", "--yes"], input=script, capture_output=True, text=True, env=environment, cwd=home)
    assert finished.returncode == 0, finished.stderr
    return finished.stdout


@only_on_linux
def test_the_installer_replaces_the_old_layout_and_knows_it_is_an_upgrade(tmp_path):
    home = tmp_path / "home"
    share = home / ".local/share/cryopilot"
    share.mkdir(parents=True)
    (home / ".local/bin").mkdir()
    old = share / "cryopilot-0.3.4-linux-x86_64"
    old.write_text("old program")
    (home / ".local/bin/cryopilot").symlink_to(old)
    (share / ".install.left").mkdir()
    keep = home / ".cryopilot/app/app.sqlite3"
    keep.parent.mkdir(parents=True)
    keep.write_text("projects")

    output = run_installer(tmp_path, home)

    assert contents(share) == [updates.PROGRAM_NAME]
    assert (home / ".local/bin/cryopilot").resolve() == share / updates.PROGRAM_NAME
    assert "keeps the old version until you restart" in output
    assert keep.read_text() == "projects"


@only_on_linux
def test_the_installer_calls_a_first_install_new(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    output = run_installer(tmp_path, home)
    assert contents(home / ".local/share/cryopilot") == [updates.PROGRAM_NAME]
    assert "keeps the old version" not in output


# ------------------------------------------------------------------ releasing
@needs_build_scripts
def test_every_release_lists_its_changes_and_the_current_version_has_notes():
    sys.path.insert(0, str(REPO_ROOT / "packaging"))
    import make_releases_json

    releases = make_releases_json.read_changelog((REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert releases[0]["version"] == updates.VERSION
    assert all(release["notes"] for release in releases)
    keys = [updates.version_key(release["version"]) for release in releases]
    assert keys == sorted(keys, reverse=True)
