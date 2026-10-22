"""Project file roots, listing, previews, and safe path resolution.

Every project has three writable roots: the modeling workspace, uploads, and
the reconstruction runs folder. Each reconstruction session bound to a
CryoSPARC project whose folder this computer can read adds a read-only root
``cryosparc:<session_id>``. Every path a client sends is resolved inside its
root; a symlink that leads out of its root is refused.
"""

from __future__ import annotations

import codecs
import csv
import fnmatch
import logging
import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import MODELING_SANDBOX_ROOT, MODELING_WORKSPACES_DIR, PROJECTS_DIR

log = logging.getLogger(__name__)

STRUCTURE_EXTENSIONS = {".pdb", ".cif", ".mmcif", ".ent", ".pdb.gz", ".cif.gz"}
VOLUME_EXTENSIONS = {".mrc", ".map", ".ccp4", ".mrc.gz", ".map.gz"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
# FASTA sequences; the attach menu and the Files panel use the same list.
SEQUENCE_EXTENSIONS = (".fasta", ".fa", ".faa", ".fas", ".fna", ".seq")
DATA_EXTENSIONS = {
    ".csv", ".tsv", ".txt", ".json", *SEQUENCE_EXTENSIONS, ".star", ".md", ".log", ".xml", ".cs",
    ".py", ".sh", ".yaml", ".yml", ".toml",
}
COMPRESSION_SUFFIXES = {".gz", ".bz2", ".xz", ".zip"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".ruff_cache"}
MAX_ENTRIES = 3000
MAX_TREE_ENTRIES = 2000
TEXT_PREVIEW_BYTES = 512 * 1024
TABLE_ROW_LIMIT = 5000
# Larger tables report the rows shown, not a full count, so a preview stays quick.
TABLE_COUNT_LIMIT_BYTES = 64 * 1024 * 1024

ROOT_KEYS = ("files", "reconstruction", "modeling")
ROOT_ORDER = ("modeling", "files", "reconstruction")
ROOT_LABELS = {"modeling": "Modeling workspace", "files": "Uploads", "reconstruction": "Reconstruction runs"}
CRYOSPARC_ROOT_PREFIX = "cryosparc:"

# Server path completion for dataset paths.
FS_LIST_LIMIT = 500
GLOB_PREVIEW = 20
GLOB_COUNT_LIMIT = 200_000
GLOB_TIME_LIMIT_S = 3.0
GLOB_CHARACTERS = set("*?[")
PSEUDO_FILESYSTEMS = ("/proc", "/sys")


def project_dir(slug: str) -> Path:
    return PROJECTS_DIR / slug


def project_reconstruction_dir(slug: str) -> Path:
    return project_dir(slug) / "reconstruction"


def project_files_dir(slug: str) -> Path:
    return project_dir(slug) / "files"


def modeling_workspace(slug: str) -> Path:
    return MODELING_WORKSPACES_DIR / slug


def modeling_workspace_relative(slug: str) -> str:
    return str(modeling_workspace(slug).relative_to(MODELING_SANDBOX_ROOT))


def ensure_project_dirs(slug: str) -> dict[str, str]:
    dirs = {
        "project": project_dir(slug),
        "files": project_files_dir(slug),
        "reconstruction": project_reconstruction_dir(slug),
        "modeling": modeling_workspace(slug),
        "modeling_outputs": modeling_workspace(slug) / "outputs",
        "modeling_data": modeling_workspace(slug) / "data",
    }
    for path in dirs.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Uploads and copied maps are the user's own data, also on a shared checkout.
    for key in ("project", "modeling"):
        try:
            os.chmod(dirs[key], 0o700)
        except OSError:
            pass
    return {key: str(path) for key, path in dirs.items()}


# ------------------------------------------------------------------- roots
def is_writable_root(root: str) -> bool:
    return root in ROOT_KEYS


def cryosparc_project_dir(session: dict[str, Any]) -> Path | None:
    """The CryoSPARC project folder of a reconstruction session, if this computer can read it."""

    try:
        from .cryosparc_jobs import project_dir_for_session
    except ImportError:
        return None
    try:
        directory = project_dir_for_session(session)
    except Exception as exc:  # an unreachable CryoSPARC must not break the file roots
        log.warning("Could not find the CryoSPARC project folder of session %s: %s", session.get("id"), exc)
        return None
    if not directory:
        return None
    directory = Path(directory)
    if directory.is_dir() and os.access(directory, os.R_OK | os.X_OK):
        return directory
    return None


def _cryosparc_label(session: dict[str, Any], directory: Path) -> str:
    config = session.get("config") or {}
    project_uid = str(config.get("project_uid") or "").strip()
    return f"CryoSPARC {project_uid or directory.name}"


def _store():
    from .state import store

    return store


def _cryosparc_root_dir(slug: str, root: str) -> Path:
    from .store import NotFound

    session_id = root[len(CRYOSPARC_ROOT_PREFIX):]
    store = _store()
    try:
        project = store.get_project(slug)
        session = store.get_session(session_id)
    except NotFound as exc:
        raise ValueError(f"Unknown file root: {root}") from exc
    if session["project_id"] != project["id"] or session["kind"] != "reconstruction":
        raise ValueError(f"Unknown file root: {root}")
    directory = cryosparc_project_dir(session)
    if directory is None:
        raise ValueError("The CryoSPARC project folder of this session is not readable on this computer")
    return directory


def root_path(slug: str, root: str) -> Path:
    if root == "files":
        return project_files_dir(slug)
    if root == "reconstruction":
        return project_reconstruction_dir(slug)
    if root == "modeling":
        return modeling_workspace(slug)
    if root.startswith(CRYOSPARC_ROOT_PREFIX):
        return _cryosparc_root_dir(slug, root)
    raise ValueError(f"Unknown file root: {root}")


def project_roots(project: dict[str, Any], sessions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every file root of a project, writable ones first."""

    slug = project["slug"]
    roots = [
        {"key": key, "label": ROOT_LABELS[key], "path": str(root_path(slug, key)), "writable": True}
        for key in ROOT_ORDER
    ]
    if sessions is None:
        sessions = _store().list_sessions(project["id"], "reconstruction")
    seen: set[Path] = set()
    for session in sessions:
        if session.get("kind") != "reconstruction":
            continue
        directory = cryosparc_project_dir(session)
        if directory is None:
            continue
        resolved = directory.resolve()
        if resolved in seen:  # a session imported twice, or resumed under a new id
            continue
        seen.add(resolved)
        roots.append(
            {
                "key": f"{CRYOSPARC_ROOT_PREFIX}{session['id']}",
                "label": _cryosparc_label(session, directory),
                "path": str(directory),
                "writable": False,
                "session_id": session["id"],
                "session_title": session.get("title") or "",
            }
        )
    return roots


# -------------------------------------------------------------- resolution
def confine(base: Path, relative: str, *, follow_symlinks: bool = True) -> Path:
    """``relative`` inside ``base`` (already resolved), refusing anything that escapes it.

    An absolute path that already lies inside ``base`` is accepted as is, so
    paths recorded by the agents (which resolve symlinks) work too. With
    ``follow_symlinks=False`` the last component is kept as named, so deleting
    a symlink removes the link and not what it points to.
    """

    text = (relative or "").strip()
    candidate = Path(text)
    if not candidate.is_absolute() or not _inside(base, candidate.resolve()):
        candidate = base / text.lstrip("/")
    if follow_symlinks:
        target = candidate.resolve()
    else:
        parent = candidate.parent.resolve()
        target = parent if candidate.name in {"", ".", ".."} else parent / candidate.name
        if candidate.name == "..":
            target = parent.parent
    if not _inside(base, target):
        raise ValueError("Path escapes its root")
    return target


def _inside(base: Path, path: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def resolve(slug: str, root: str, relative: str, *, follow_symlinks: bool = True) -> Path:
    base = root_path(slug, root).resolve()
    return confine(base, relative, follow_symlinks=follow_symlinks)


def relative_path(slug: str, root: str, path: Path) -> str:
    base = root_path(slug, root).resolve()
    relative = path.relative_to(base).as_posix()
    return "" if relative == "." else relative


# -------------------------------------------------------------- categories
def file_category(name: str) -> str:
    lower = name.lower()
    for ext in sorted(STRUCTURE_EXTENSIONS, key=len, reverse=True):
        if lower.endswith(ext):
            return "structure"
    for ext in sorted(VOLUME_EXTENSIONS, key=len, reverse=True):
        if lower.endswith(ext):
            return "volume"
    for ext in IMAGE_EXTENSIONS:
        if lower.endswith(ext):
            return "image"
    for ext in DATA_EXTENSIONS:
        if lower.endswith(ext):
            return "data"
    return "other"


def structure_format(name: str) -> str:
    lower = name.lower()
    if lower.endswith((".cif", ".mmcif", ".cif.gz")):
        return "mmcif"
    return "pdb"


def numbered_name(name: str, index: int) -> str:
    """``<stem>-<index><suffix>``, keeping a compressed file's two suffixes together (``map-2.mrc.gz``)."""

    path = Path(name)
    suffix = path.suffix
    if suffix.lower() in COMPRESSION_SUFFIXES and Path(path.stem).suffix:
        suffix = Path(path.stem).suffix + suffix
    return f"{name[: len(name) - len(suffix)]}-{index}{suffix}"


def guess_mime(path: Path) -> str:
    name = path.name.lower()
    if name.endswith((".mrc", ".map", ".ccp4")):
        return "application/octet-stream"
    if name.endswith((".pdb", ".ent")):
        return "chemical/x-pdb"
    if name.endswith((".cif", ".mmcif")):
        return "chemical/x-mmcif"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


# ----------------------------------------------------------------- listing
def list_tree(slug: str, root: str) -> list[dict[str, Any]]:
    """Every file under one of the writable roots, flattened (the old ``GET /files``)."""

    base = root_path(slug, root)
    if not base.exists():
        return []
    entries: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            full = Path(dirpath) / filename
            try:
                stat = full.stat()
            except OSError:
                continue
            relative = str(full.relative_to(base))
            entries.append(
                {
                    "root": root,
                    "path": relative,
                    "name": filename,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "category": file_category(filename),
                }
            )
            if len(entries) >= MAX_ENTRIES:
                return entries
    return entries


def list_directory(slug: str, root: str, relative: str = "") -> dict[str, Any]:
    """One directory level: folders first, then files, by name; hidden entries skipped."""

    base = root_path(slug, root).resolve()
    directory = confine(base, relative)
    if not directory.exists():
        raise FileNotFoundError("Folder not found")
    if not directory.is_dir():
        raise ValueError("Not a folder")
    candidates: list[tuple[bool, str, os.DirEntry]] = []
    with os.scandir(directory) as scan:
        for entry in scan:
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir and entry.name in SKIP_DIRS:
                continue
            candidates.append((is_dir, entry.name, entry))
    candidates.sort(key=lambda item: (not item[0], item[1].lower(), item[1]))

    entries: list[dict[str, Any]] = []
    truncated = False
    for is_dir, name, entry in candidates:
        if len(entries) >= MAX_TREE_ENTRIES:
            truncated = True
            break
        try:
            if entry.is_symlink() and not _inside(base, Path(entry.path).resolve()):
                continue  # it could not be opened through this root anyway
            stat = entry.stat()
        except OSError:
            continue  # broken symlink or vanished file
        entries.append(
            {
                "name": name,
                "path": (directory / name).relative_to(base).as_posix(),
                "type": "dir" if is_dir else "file",
                "size": None if is_dir else stat.st_size,
                "modified": stat.st_mtime,
                "category": "dir" if is_dir else file_category(name),
            }
        )
    current = directory.relative_to(base).as_posix()
    return {"root": root, "path": "" if current == "." else current, "entries": entries, "truncated": truncated}


# ---------------------------------------------------------------- previews
def read_text(path: Path, limit: int = TEXT_PREVIEW_BYTES) -> dict[str, Any]:
    """The start of a text file as UTF-8 (invalid bytes replaced)."""

    size = path.stat().st_size
    with path.open("rb") as handle:
        raw = handle.read(limit)
    truncated = size > len(raw)
    # A cut may split a multi-byte character; the incremental decoder holds it back.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(raw, final=not truncated)
    text = text.removeprefix("\ufeff")  # a byte-order mark
    return {"text": text, "truncated": truncated, "size": size, "binary": b"\x00" in raw[:8192]}


def _table_delimiter(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".tsv", ".tab"}:
        return "\t"
    if suffix == ".csv":
        return ","
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(64 * 1024)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def read_table(path: Path, limit: int = TABLE_ROW_LIMIT) -> dict[str, Any]:
    """A CSV/TSV file as a header plus up to ``limit`` rows of strings."""

    size = path.stat().st_size
    delimiter = _table_delimiter(path)
    columns: list[str] = []
    rows: list[list[str]] = []
    total: int | None = 0
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            columns = next(reader, [])
            for row in reader:
                if not row:
                    continue
                total += 1
                if len(rows) < limit:
                    rows.append(row)
                elif size > TABLE_COUNT_LIMIT_BYTES:
                    total = None
                    break
    except csv.Error as exc:
        raise ValueError(f"Could not read this file as a table: {exc}") from exc
    return {
        "columns": columns,
        "rows": rows,
        "total_rows": total,
        "truncated": total is None or total > len(rows),
        "delimiter": delimiter,
        "size": size,
    }


# ----------------------------------------------------------- write access
def save_upload(source, target: Path) -> None:
    """Copy an uploaded file into place without exposing a half-written file."""

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.uploading")
    try:
        with partial.open("wb") as handle:
            shutil.copyfileobj(source, handle, 1024 * 1024)
        os.replace(partial, target)
    finally:
        if partial.exists():
            partial.unlink()


def delete_path(slug: str, root: str, relative: str) -> None:
    base = root_path(slug, root).resolve()
    target = confine(base, relative, follow_symlinks=False)
    if target == base:
        raise ValueError("The root folder itself cannot be deleted")
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        raise FileNotFoundError("File not found")


# --------------------------------------------------- server path completion
def _pseudo_filesystem(real: str) -> str | None:
    """The pseudo filesystem (/proc, /sys) that a resolved path lies in, if any."""

    for blocked in PSEUDO_FILESYSTEMS:
        if real == blocked or real.startswith(blocked + "/"):
            return blocked
    return None


def _refuse_pseudo_filesystem(path: str) -> None:
    blocked = _pseudo_filesystem(os.path.realpath(path))
    if blocked:
        raise ValueError(f"CryoPilot does not browse {blocked}.")


def _server_entry(path: str, name: str | None = None) -> dict[str, Any] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    is_dir = os.path.isdir(path)
    return {
        "name": name or os.path.basename(path.rstrip("/")) or path,
        "path": path,
        "type": "dir" if is_dir else "file",
        "size": None if is_dir else stat.st_size,
        "modified": stat.st_mtime,
    }


def _expand_server_path(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return str(Path.home()) + "/"
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(str(Path.home()), expanded)
    return expanded


def _read_folder_error(directory: str, exc: OSError) -> ValueError:
    if isinstance(exc, PermissionError):
        return ValueError(f"You do not have permission to read {directory}")
    if isinstance(exc, NotADirectoryError):
        return ValueError(f"{directory} is not a folder")
    if isinstance(exc, FileNotFoundError):
        return ValueError(f"No folder at {directory}")
    return ValueError(f"Could not read {directory}: {exc.strerror or exc}")


def list_server_path(text: str, dirs_only: bool = False) -> dict[str, Any]:
    """Completions for a path typed into a dataset-path field.

    An existing folder lists its contents. A partial path lists the parent
    folder's entries that start with the typed name. A glob pattern
    (``/data/*.tif``) reports how many files match and the first few.
    """

    expanded = _expand_server_path(text)
    if GLOB_CHARACTERS & set(expanded):
        return _glob_server_path(expanded, dirs_only)

    if expanded.endswith("/") or os.path.isdir(expanded):
        directory, prefix = os.path.normpath(expanded), ""
    else:
        directory, prefix = os.path.dirname(os.path.normpath(expanded)) or "/", os.path.basename(expanded)
    _refuse_pseudo_filesystem(directory)
    try:
        with os.scandir(directory) as scan:
            names = [(entry.name, entry) for entry in scan]
    except OSError as exc:
        raise _read_folder_error(directory, exc) from exc

    show_hidden = prefix.startswith(".")
    visible = [(name, entry) for name, entry in names if show_hidden or not name.startswith(".")]
    matches = [(name, entry) for name, entry in visible if name.startswith(prefix)]
    if prefix and not matches:
        matches = [(name, entry) for name, entry in visible if name.lower().startswith(prefix.lower())]

    candidates = []
    for name, entry in matches:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if dirs_only and not is_dir:
            continue
        candidates.append((not is_dir, name.lower(), name))
    candidates.sort()

    entries = []
    for _, _, name in candidates[:FS_LIST_LIMIT]:
        entry = _server_entry(os.path.join(directory, name), name)
        if entry is not None:
            entries.append(entry)
    exists = os.path.exists(expanded)
    return {
        "path": expanded,
        "parent": directory,
        "prefix": prefix,
        "exists": exists,
        "type": ("dir" if os.path.isdir(expanded) else "file") if exists else None,
        "glob": False,
        "entries": entries,
        "truncated": len(candidates) > FS_LIST_LIMIT,
    }


def _glob_walk(directory: str, components: list[str], dirs_only: bool) -> tuple[list[str], bool]:
    """Paths under ``directory`` matching ``components`` (one pattern per level), as ``glob`` finds them.

    The walk checks the time limit at every directory entry, so a pattern that
    matches nothing in a large tree still stops in time, and it never enters
    /proc or /sys, also not through a symlink. Returns the matches and whether
    the search stopped early.
    """

    deadline = time.monotonic() + GLOB_TIME_LIMIT_S
    matches: list[str] = []
    last = len(components) - 1
    stack = [(directory, os.path.realpath(directory), 0)]
    while stack:
        if time.monotonic() > deadline:
            return matches, True
        folder, real_folder, level = stack.pop()
        component = components[level]
        found: list[tuple[str, str]] = []
        if not GLOB_CHARACTERS & set(component):
            path = os.path.join(folder, component)
            if os.path.lexists(path):
                found.append((path, os.path.realpath(path)))
        else:
            try:
                scan = os.scandir(folder)
            except OSError:
                continue
            with scan:
                for entry in scan:
                    if time.monotonic() > deadline:
                        return matches, True
                    # As in glob, a wildcard does not match hidden names unless the pattern starts with a dot.
                    if entry.name.startswith(".") and not component.startswith("."):
                        continue
                    if not fnmatch.fnmatchcase(entry.name, component):
                        continue
                    try:
                        linked = entry.is_symlink()
                    except OSError:
                        continue
                    real = os.path.realpath(entry.path) if linked else os.path.join(real_folder, entry.name)
                    found.append((entry.path, real))
        for path, real in found:
            if _pseudo_filesystem(real):
                continue
            if level < last:
                if os.path.isdir(path):
                    stack.append((path, real, level + 1))
                continue
            if dirs_only and not os.path.isdir(path):
                continue
            matches.append(path)
            if len(matches) >= GLOB_COUNT_LIMIT:
                return matches, True
    return matches, False


def _glob_server_path(pattern: str, dirs_only: bool) -> dict[str, Any]:
    parts = Path(pattern).parts
    static = []
    for part in parts:
        if GLOB_CHARACTERS & set(part):
            break
        static.append(part)
    if len(static) < 2:
        # A wildcard right below / would search the whole computer.
        raise ValueError("Start the pattern with a folder, for example /data/movies/*.tif")
    directory = str(Path(*static))
    _refuse_pseudo_filesystem(directory)
    if not os.path.isdir(directory):
        raise ValueError(f"No folder at {directory}")
    if not os.access(directory, os.R_OK | os.X_OK):
        raise ValueError(f"You do not have permission to read {directory}")

    # A trailing slash asks for folders, as it does for glob.
    matches, capped = _glob_walk(directory, list(parts[len(static):]), dirs_only or pattern.endswith("/"))
    matches.sort()
    entries = [entry for entry in (_server_entry(match) for match in matches[:GLOB_PREVIEW]) if entry]
    return {
        "path": pattern,
        "parent": directory,
        "prefix": "",
        "exists": bool(matches),
        "type": None,
        "glob": True,
        "match_count": len(matches),
        "count_capped": capped,
        "entries": entries,
        "truncated": len(matches) > len(entries),
    }
