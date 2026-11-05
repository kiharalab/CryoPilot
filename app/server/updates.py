"""Find newer versions of CryoPilot, install one, and remove what old versions left.

The website publishes releases.json (written by packaging/make_releases_json.py):
the newest release with its download address, checksum, and size, and the notes
of every release. The server reads it once a day; nothing but the request itself
is sent. CRYOPILOT_NO_UPDATE_CHECK=1 turns the daily check off.

An update changes nothing until the new program has passed its checks. It is
downloaded next to the current program, so that the last step is one rename on
the same disk, and it must match the published checksum and start before that
rename happens. A failed update leaves the current version as it was and removes
its own files. It touches only the program file and, afterwards, the unpacked
folders of older versions: projects, sessions, settings, and shell startup files
stay as they are.

A folder of an older version goes only when nothing can still be running from
it. Processes on this computer are found in /proc. A home folder can be shared
between computers, whose processes are not visible here, so each server also
marks its folder regularly, and a recent mark from another computer keeps it.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import certifi

from ..version import VERSION
from .config import APP_HOME

RELEASES_URL = os.environ.get("CRYOPILOT_RELEASES_URL", "https://cryopilot.vercel.app/releases.json")
INSTALL_COUNT_URL = "https://cryopilot.vercel.app/api/installs"
CACHE_PATH = APP_HOME / "releases.json"
CHECK_EVERY = 24 * 3600
CHECK_TIMEOUT = 5
MAX_MANIFEST_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT = 60
# The first start of the new program unpacks it, which is slow on a network disk.
RUN_TIMEOUT = 15 * 60
PROGRAM_NAME = "cryopilot-linux-x86_64"
PARTIAL_NAME = ".cryopilot-update.part"
PARTIAL_STALE = 30 * 60
SHA256 = re.compile(r"[0-9a-f]{64}")
VERSION_TEXT = re.compile(r"\d+\.\d+\.\d+")
# The unpacked folders that packaging/launcher.sh makes; nothing else is ever removed.
UNPACKED_FOLDER = re.compile(r"(\d+\.\d+\.\d+)-[0-9a-f]{12}")
UNPACK_STALE = 24 * 3600
MARK_PREFIX = ".used-on-"
MARK_EVERY = 5 * 60
MARK_STALE = 30 * 60
# Versions before 0.3.5 leave no marks, so another computer could still be using the folder.
UNMARKED_KEEP = 7 * 24 * 3600
SWEEP_EVERY = 3600
# packaging/cryopilot.py runs remove_old_versions() for this argument.
REMOVE_OLD_ARGUMENT = "--remove-old-versions"


class UpdateError(Exception):
    """An update could not be finished. The message is written for the person updating."""


def version_key(text: Any) -> tuple[int, ...] | None:
    if not isinstance(text, str) or not VERSION_TEXT.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def _open(url: str, timeout: float, data: bytes | None = None):
    request = urllib.request.Request(url, data=data, headers={"User-Agent": f"CryoPilot/{VERSION}"})
    # The packaged program cannot rely on finding this computer's certificates.
    return urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context(cafile=certifi.where()))


# ------------------------------------------------------------------ checking
def _valid_manifest(manifest: Any) -> bool:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("releases"), list):
        return False
    latest = manifest.get("latest")
    return isinstance(latest, dict) and version_key(latest.get("version")) is not None


def _read_cache() -> dict[str, Any]:
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) and _valid_manifest(cache.get("manifest")) else {}


def cached_manifest() -> dict[str, Any] | None:
    if os.environ.get("CRYOPILOT_NO_UPDATE_CHECK") == "1":
        return None
    return _read_cache().get("manifest")


def fetch_manifest() -> dict[str, Any]:
    """Asks the website for releases.json and keeps it. Raises UpdateError when that fails."""

    try:
        with _open(RELEASES_URL, CHECK_TIMEOUT) as response:
            manifest = json.loads(response.read(MAX_MANIFEST_BYTES))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise UpdateError(f"Could not read {RELEASES_URL}: {exc}") from exc
    if not _valid_manifest(manifest):
        raise UpdateError(f"{RELEASES_URL} is not a list of releases.")
    try:
        APP_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = CACHE_PATH.with_name(f".{CACHE_PATH.name}.tmp")
        temporary.write_text(json.dumps({"checked_at": time.time(), "manifest": manifest}), encoding="utf-8")
        os.replace(temporary, CACHE_PATH)
    except OSError:
        pass
    return manifest


def check_daily() -> None:
    """The background check. Computers without internet access just never see a notice."""

    if os.environ.get("CRYOPILOT_NO_UPDATE_CHECK") == "1":
        return
    if time.time() - float(_read_cache().get("checked_at") or 0) < CHECK_EVERY:
        return
    try:
        fetch_manifest()
    except UpdateError:
        pass


def newer_release(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    latest = manifest["latest"] if manifest else None
    if latest is None or version_key(latest["version"]) <= version_key(VERSION):
        return None
    return latest


def notice() -> str | None:
    """One line for the terminal when the last check found a newer version."""

    release = newer_release(cached_manifest())
    if release is None:
        return None
    return f"CryoPilot {release['version']} is available (you have {VERSION}). Update with:  cryopilot update"


def notes_since_current(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    current = version_key(VERSION)
    return [
        release
        for release in manifest["releases"]
        if isinstance(release, dict) and (version_key(release.get("version")) or ()) > current
    ]


# ---------------------------------------------------------------- installing
def program_path() -> Path | None:
    """The file this program started from; packaging/launcher.sh sets the variable."""

    value = os.environ.get("CRYOPILOT_BINARY")
    return Path(value) if value else None


def install_blocker() -> str | None:
    """Why this copy cannot update itself, or None."""

    program = program_path()
    if program is None:
        return "This copy of CryoPilot runs from source. Update it with git."
    if not os.access(program.parent, os.W_OK):
        return f"You cannot write to {program.parent}, where CryoPilot is installed. Ask the person who installed it to update it."
    return None


def own_folder() -> Path | None:
    """The unpacked folder this program runs from, or None when it runs from source."""

    folder = Path(sys.executable).resolve().parent
    return folder if UNPACKED_FOLDER.fullmatch(folder.name) else None


def _check_release(release: dict[str, Any]) -> None:
    url = urllib.parse.urlsplit(str(release.get("url") or ""))
    encrypted = url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"})
    if (
        version_key(release.get("version")) is None
        or not encrypted
        or not SHA256.fullmatch(str(release.get("sha256") or ""))
        or not isinstance(release.get("size"), int)
    ):
        raise UpdateError("The list of releases on the website is not complete. Try again later.")


def _claim(partial: Path) -> None:
    """Creates the download file, so that two updates cannot run at the same time."""

    for _ in range(2):
        try:
            os.close(os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return
        except FileExistsError:
            # An update that was killed cannot remove its own file.
            if time.time() - partial.stat().st_mtime < PARTIAL_STALE:
                raise UpdateError(f"Another update is running. If none is, delete {partial} and try again.") from None
            partial.unlink(missing_ok=True)
    raise UpdateError(f"Another update is running. If none is, delete {partial} and try again.")


def _require_space(folder: Path, needed_mb: int) -> None:
    free_mb = shutil.disk_usage(folder).free // 1024 ** 2
    if free_mb < needed_mb:
        raise UpdateError(f"{folder} has {free_mb} MB free, and the update needs {needed_mb} MB. Nothing was changed.")


def _download(release: dict[str, Any], partial: Path, on_progress: Callable[[int, int], None]) -> None:
    digest = hashlib.sha256()
    done = 0
    try:
        with _open(release["url"], DOWNLOAD_TIMEOUT) as response, open(partial, "wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                on_progress(done, release["size"])
    except (OSError, http.client.HTTPException) as exc:
        raise UpdateError(f"The download failed: {exc}. Nothing was changed.") from exc
    if digest.hexdigest() != release["sha256"]:
        raise UpdateError("The download is damaged (its checksum does not match). Nothing was changed.")


def _check_it_starts(partial: Path, version: str) -> None:
    os.chmod(partial, 0o755)
    os.utime(partial)
    try:
        completed = subprocess.run([str(partial), "--version"], capture_output=True, text=True, timeout=RUN_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"The new version did not start: {exc}. Nothing was changed.") from exc
    if completed.stdout.strip() != f"cryopilot {version}":
        message = completed.stderr.strip().splitlines()[-1:] or [f"exit code {completed.returncode}"]
        raise UpdateError(f"The new version did not start: {message[0]}. Nothing was changed.")


def _unpacked_folders(version: str) -> set[Path]:
    own = own_folder()
    if own is None:
        return set()
    return {folder for folder in own.parent.glob(f"{version}-*") if folder != own and UNPACKED_FOLDER.fullmatch(folder.name)}


def install(release: dict[str, Any], on_progress: Callable[[int, int], None] = lambda done, total: None) -> Path:
    """Replaces the program with this release and returns the new program's path.

    Running servers are not affected: they run from their unpacked folder and
    use the new version only after a restart.
    """

    blocker = install_blocker()
    if blocker:
        raise UpdateError(blocker)
    _check_release(release)
    program = program_path()
    version = release["version"]
    _require_space(program.parent, release["size"] // 1024 ** 2 + 1)
    if (own := own_folder()) is not None:
        _require_space(own.parent, int(release.get("unpacked_mb") or 0))
    partial = program.parent / PARTIAL_NAME
    _claim(partial)
    try:
        _download(release, partial, on_progress)
        unpacked_before = _unpacked_folders(version)
        try:
            _check_it_starts(partial, version)
        except UpdateError:
            for folder in _unpacked_folders(version) - unpacked_before:
                shutil.rmtree(folder, ignore_errors=True)
            raise
        # A copy someone downloaded by hand keeps the version in its name.
        target = program if program.name == PROGRAM_NAME else program.with_name(f"cryopilot-{version}-linux-x86_64")
        os.replace(partial, target)
        if target != program:
            program.unlink(missing_ok=True)
        return target
    finally:
        partial.unlink(missing_ok=True)


def count_install(version: str) -> None:
    """Counts the update as install.sh counts an upgrade; the update does not depend on it."""

    if os.environ.get("CRYOPILOT_NO_ANALYTICS") == "1":
        return
    data = urllib.parse.urlencode({"version": version, "kind": "upgrade"}).encode()
    try:
        _open(INSTALL_COUNT_URL, CHECK_TIMEOUT, data=data).close()
    except OSError:
        pass


# ------------------------------------------------------- what old versions left
def _used_here(folder: Path) -> bool:
    prefix = f"{folder.resolve()}{os.sep}"
    for process in Path("/proc").glob("[0-9]*"):
        try:
            # The command line too: under an emulator, the executable is the emulator.
            if os.readlink(process / "exe").startswith(prefix) or prefix.encode() in (process / "cmdline").read_bytes():
                return True
        except OSError:
            continue
    return False


def _used_elsewhere(folder: Path, now: float) -> bool:
    marks = list(folder.glob(f"{MARK_PREFIX}*"))
    this_computer = f"{MARK_PREFIX}{socket.gethostname()}"
    try:
        if not marks:
            return now - (folder / ".complete").stat().st_mtime < UNMARKED_KEEP
        return any(mark.name != this_computer and now - mark.stat().st_mtime < MARK_STALE for mark in marks)
    except OSError:
        # No .complete: a removal that stopped halfway.
        return False


def remove_old_versions() -> list[str]:
    """Removes the unpacked folders of older versions that nothing uses. Returns their versions."""

    own = own_folder()
    if own is None:
        return []
    now = time.time()
    current = version_key(VERSION)
    removed = []
    for entry in own.parent.iterdir():
        if entry == own or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            if entry.name.startswith(".unpack-"):
                # A start that was killed while unpacking.
                if now - entry.stat().st_mtime > UNPACK_STALE:
                    shutil.rmtree(entry, ignore_errors=True)
                continue
            match = UNPACKED_FOLDER.fullmatch(entry.name)
            if match is None or version_key(match[1]) > current:
                continue
            if _used_here(entry) or _used_elsewhere(entry, now):
                continue
            # First, so that a removal that stops halfway is never taken for a whole folder.
            (entry / ".complete").unlink(missing_ok=True)
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed.append(match[1])
        except OSError:
            continue
    return removed


def mark_in_use() -> None:
    own = own_folder()
    if own is None:
        return
    try:
        (own / f"{MARK_PREFIX}{socket.gethostname()}").touch()
    except OSError:
        pass


def _background() -> None:
    next_sweep = 0.0
    while True:
        mark_in_use()
        if time.monotonic() >= next_sweep:
            next_sweep = time.monotonic() + SWEEP_EVERY
            try:
                remove_old_versions()
                check_daily()
            except Exception as exc:
                print(f"The update check failed: {exc}", file=sys.stderr, flush=True)
        time.sleep(MARK_EVERY)


def start_background() -> None:
    threading.Thread(target=_background, name="cryopilot-updates", daemon=True).start()


# -------------------------------------------------------- the app's update button
_lock = threading.Lock()
_job: dict[str, Any] = {"state": "idle", "version": None, "done": 0, "total": 0, "error": None}
_new_program: Path | None = None
# serve.py sets this to stop the server, and starts the new program once it has stopped.
stop_server: Callable[[], None] | None = None
restart_requested = False


def status() -> dict[str, Any]:
    manifest = cached_manifest()
    release = newer_release(manifest)
    with _lock:
        job = dict(_job)
    return {
        "current": VERSION,
        "latest": release["version"] if release else None,
        "size": release.get("size") if release else None,
        "releases": manifest["releases"] if manifest else [],
        "new_releases": notes_since_current(manifest) if manifest else [],
        "blocker": install_blocker(),
        "install": job,
    }


def _run_install(release: dict[str, Any]) -> None:
    global _new_program

    def progress(done: int, total: int) -> None:
        with _lock:
            _job.update(done=done, total=total, state="downloading" if done < total else "checking")

    try:
        program = install(release, progress)
    except UpdateError as exc:
        with _lock:
            _job.update(state="failed", error=str(exc))
    except Exception as exc:
        with _lock:
            _job.update(state="failed", error=f"The update failed: {exc}. Nothing was changed.")
    else:
        count_install(release["version"])
        with _lock:
            _new_program = program
            _job.update(state="ready")


def start_install() -> None:
    release = newer_release(cached_manifest())
    if release is None:
        raise UpdateError("This is the latest version.")
    blocker = install_blocker()
    if blocker:
        raise UpdateError(blocker)
    with _lock:
        if _job["state"] in {"downloading", "checking", "ready"}:
            return
        _job.update(state="downloading", version=release["version"], done=0, total=release.get("size") or 0, error=None)
    threading.Thread(target=_run_install, args=(release,), name="cryopilot-update", daemon=True).start()


def restart() -> None:
    """Stops the server, after which serve.py starts the new program in its place."""

    global restart_requested
    if _new_program is None or stop_server is None:
        raise UpdateError("There is no installed update to restart into.")
    restart_requested = True
    # After the reply has reached the browser.
    threading.Timer(0.5, stop_server).start()


def new_program() -> Path | None:
    return _new_program
