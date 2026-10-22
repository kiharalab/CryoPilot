"""SQLite-backed application state: projects, sessions, and modeling events.

The reconstruction transcript itself is owned by the harness session database.
This store only records which harness session belongs to which project, plus
the complete modeling-agent event log, which the modeling agent does not
persist on its own.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "project"


class AppStoreError(RuntimeError):
    pass


class NotFound(AppStoreError):
    pass


class AppStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------ schema
    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    parent_directory TEXT NOT NULL DEFAULT '',
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('reconstruction', 'modeling')),
                    title TEXT NOT NULL,
                    harness_session_id TEXT,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project_id, kind, created_at);
                CREATE TABLE IF NOT EXISTS modeling_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS modeling_events_session ON modeling_events(session_id, id);
                CREATE TABLE IF NOT EXISTS console_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    stream TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS console_lines_session ON console_lines(session_id, id);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                """
            )

    # ---------------------------------------------------------------- projects
    @staticmethod
    def _project(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "description": row["description"],
            "parent_directory": row["parent_directory"],
            "settings": json.loads(row["settings_json"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC"
            ).fetchall()
            projects = [self._project(row) for row in rows]
            for project in projects:
                counts = self._connection.execute(
                    "SELECT kind, COUNT(*) AS n FROM sessions WHERE project_id = ? GROUP BY kind",
                    (project["id"],),
                ).fetchall()
                project["session_counts"] = {row["kind"]: row["n"] for row in counts}
                latest = self._connection.execute(
                    "SELECT MAX(updated_at) AS t FROM sessions WHERE project_id = ?",
                    (project["id"],),
                ).fetchone()
                project["last_activity"] = latest["t"] or project["updated_at"]
            return projects

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM projects WHERE id = ? OR slug = ?", (project_id, project_id)
            ).fetchone()
        if row is None:
            raise NotFound(f"Project {project_id} not found")
        return self._project(row)

    def create_project(
        self,
        *,
        title: str,
        description: str = "",
        parent_directory: str = "",
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise AppStoreError("A project title is required")
        base_slug = slugify(title)
        now = utcnow()
        project_id = uuid.uuid4().hex[:12]
        with self._lock:
            slug = base_slug
            suffix = 2
            while self._connection.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                slug = f"{base_slug}-{suffix}"
                suffix += 1
            self._connection.execute(
                """
                INSERT INTO projects (id, slug, title, description, parent_directory, settings_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    slug,
                    title,
                    description.strip(),
                    parent_directory.strip(),
                    json.dumps(settings or {}),
                    now,
                    now,
                ),
            )
        return self.get_project(project_id)

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"title", "description", "parent_directory", "settings"}
        updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
        if not updates:
            return self.get_project(project_id)
        assignments = []
        values: list[Any] = []
        for key, value in updates.items():
            if key == "settings":
                assignments.append("settings_json = ?")
                values.append(json.dumps(value))
            else:
                assignments.append(f"{key} = ?")
                values.append(str(value).strip())
        assignments.append("updated_at = ?")
        values.append(utcnow())
        values.append(project_id)
        with self._lock:
            self._connection.execute(
                f"UPDATE projects SET {', '.join(assignments)} WHERE id = ?", values
            )
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def touch_project(self, project_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (utcnow(), project_id)
            )

    # ---------------------------------------------------------------- sessions
    @staticmethod
    def _session(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "title": row["title"],
            "harness_session_id": row["harness_session_id"],
            "config": json.loads(row["config_json"] or "{}"),
            "status": row["status"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_sessions(self, project_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM sessions WHERE project_id = ?"
        params: list[Any] = [project_id]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._session(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise NotFound(f"Session {session_id} not found")
        return self._session(row)

    def find_session_by_harness_id(self, harness_session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE harness_session_id = ?", (harness_session_id,)
            ).fetchone()
        return None if row is None else self._session(row)

    def create_session(
        self,
        *,
        project_id: str,
        kind: str,
        title: str,
        config: dict[str, Any] | None = None,
        harness_session_id: str | None = None,
        status: str = "idle",
    ) -> dict[str, Any]:
        self.get_project(project_id)
        now = utcnow()
        session_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO sessions (id, project_id, kind, title, harness_session_id, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    project_id,
                    kind,
                    title.strip() or f"{kind.title()} session",
                    harness_session_id,
                    json.dumps(config or {}),
                    status,
                    now,
                    now,
                ),
            )
            self._connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
            )
        return self.get_session(session_id)

    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"title", "harness_session_id", "config", "status", "last_error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_session(session_id)
        assignments = []
        values: list[Any] = []
        for key, value in updates.items():
            if key == "config":
                assignments.append("config_json = ?")
                values.append(json.dumps(value or {}))
            else:
                assignments.append(f"{key} = ?")
                values.append(value)
        assignments.append("updated_at = ?")
        values.append(utcnow())
        values.append(session_id)
        with self._lock:
            self._connection.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?", values
            )
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # --------------------------------------------------------- modeling events
    def append_modeling_event(self, session_id: str, kind: str, payload: Any) -> dict[str, Any]:
        now = utcnow()
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO modeling_events (session_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (session_id, kind, json.dumps(payload, ensure_ascii=False), now),
            )
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
            event_id = cursor.lastrowid
        return {"id": event_id, "kind": kind, "payload": payload, "created_at": now}

    def list_modeling_events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM modeling_events WHERE session_id = ? AND id > ? ORDER BY id",
                (session_id, after),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------ console log
    def append_console_line(self, session_id: str, stream: str, text: str) -> dict[str, Any]:
        now = utcnow()
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO console_lines (session_id, stream, text, created_at) VALUES (?, ?, ?, ?)",
                (session_id, stream, text, now),
            )
            line_id = cursor.lastrowid
        return {"id": line_id, "stream": stream, "text": text, "created_at": now}

    def list_console_lines(self, session_id: str, after: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM console_lines WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
                (session_id, after, limit),
            ).fetchall()
        return [
            {"id": row["id"], "stream": row["stream"], "text": row["text"], "created_at": row["created_at"]}
            for row in rows
        ]

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO settings (key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (key, json.dumps(value)),
            )
