"""Durable, provider-neutral session storage for the cryo-EM agent.

The store deliberately persists JSON values rather than SDK objects.  This keeps
the transcript replayable across SDK/provider upgrades and preserves response
fields that this version of the application does not yet understand.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 2
DEFAULT_DATABASE_PATH = Path("~/.cryoagent/sessions.sqlite3").expanduser()
DEFAULT_RECURSION_CONFIG = {
    "max_depth": 4,
    "max_calls": 16,
    "max_model_turns": 64,
}
SESSION_STATUSES = frozenset(
    {"collecting", "initializing", "running", "interrupted", "completed", "error"}
)
RECURSIVE_CALL_STATUSES = frozenset(
    {"running", "completed", "failed", "interrupted", "budget_exhausted"}
)
RECURSIVE_MODEL_TURN_STATUSES = frozenset(
    {"reserved", "completed", "failed", "interrupted"}
)
RECURSIVE_MODEL_TURN_PURPOSES = frozenset({"agent", "compaction"})


class SessionStoreError(RuntimeError):
    """Base class for persistent-session failures."""


class SessionNotFoundError(SessionStoreError):
    """Raised when a requested session ID does not exist."""


class SchemaVersionError(SessionStoreError):
    """Raised when a database has an unsupported or malformed schema."""


class DatabaseCorruptionError(SessionStoreError):
    """Raised when SQLite or stored JSON reports corrupt persistent state."""


class RecursiveCallNotFoundError(SessionStoreError):
    """Raised when a requested recursive call or model turn does not exist."""


class RecursiveLimitError(SessionStoreError):
    """Raised before inference when a persisted recursive budget is exhausted."""


@dataclass(frozen=True)
class SessionRecord:
    id: str
    created_at: str
    updated_at: str
    status: str
    provider: str
    model: str
    reasoning: Any
    compact_config: Any
    recursion_config: Any
    cryosparc_url: str | None
    cryosparc_email: str | None
    project_uid: str | None
    workspace_uid: str | None
    manifest: Any
    last_error: str | None


@dataclass(frozen=True)
class ConversationItemRecord:
    id: int
    session_id: str
    sequence: int
    origin: str
    payload: Any
    created_at: str


@dataclass(frozen=True)
class CheckpointRecord:
    id: int
    session_id: str
    ordinal: int
    active_item_ids: tuple[int, ...]
    last_summary_item_id: int | None
    durable_state: Any
    created_at: str


@dataclass(frozen=True)
class CompactionRecord:
    id: int
    session_id: str
    ordinal: int
    reason: str
    summary_item_id: int
    before_active_item_ids: tuple[int, ...]
    after_active_item_ids: tuple[int, ...]
    metrics: Any
    created_at: str


@dataclass(frozen=True)
class RecursiveCallRecord:
    id: int
    session_id: str
    root_turn_item_id: int
    root_function_call_item_id: int
    root_function_call_id: str
    parent_call_id: int | None
    invoking_call_id: str
    depth: int
    ordinal: int
    status: str
    active_item_ids: tuple[int, ...]
    final_output: Any
    metrics: Any
    error: Any
    started_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class RecursiveModelTurnRecord:
    id: int
    session_id: str
    call_id: int
    root_turn_item_id: int
    ordinal: int
    aggregate_ordinal: int
    purpose: str
    status: str
    active_item_ids: tuple[int, ...]
    response_item_ids: tuple[int, ...]
    metrics: Any
    error: Any
    started_at: str
    completed_at: str | None


@dataclass(frozen=True)
class RecursiveItemRecord:
    id: int
    session_id: str
    call_id: int
    sequence: int
    model_turn_id: int | None
    origin: str
    payload: Any
    created_at: str


@dataclass(frozen=True)
class RecursiveCallSnapshot:
    call: RecursiveCallRecord
    items: tuple[RecursiveItemRecord, ...]
    model_turns: tuple[RecursiveModelTurnRecord, ...]
    descendants: tuple[RecursiveCallRecord, ...]

    @property
    def history(self) -> list[Any]:
        """Return recursive payloads in their durable append order."""

        return [item.payload for item in self.items]


@dataclass(frozen=True)
class SessionSnapshot:
    session: SessionRecord
    conversation_items: tuple[ConversationItemRecord, ...]
    checkpoint: CheckpointRecord | None
    active_items: tuple[ConversationItemRecord, ...]
    last_summary_item: ConversationItemRecord | None
    compactions: tuple[CompactionRecord, ...]

    @property
    def full_history(self) -> list[Any]:
        """Return transcript payloads in their original append order."""

        return [item.payload for item in self.conversation_items]

    @property
    def active_messages(self) -> list[Any]:
        """Return payloads in the active-context order saved at checkpoint."""

        return [item.payload for item in self.active_items]

    @property
    def last_summary(self) -> Any | None:
        return None if self.last_summary_item is None else self.last_summary_item.payload


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_dump(value: Any) -> str:
    # Compact JSON avoids needless database growth.  No default serializer is
    # provided: silently stringifying an SDK object would destroy replayability.
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _json_load(value: str, *, context: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DatabaseCorruptionError(f"Invalid JSON stored for {context}") from exc


def _normalize_recursion_config(value: Mapping[str, Any] | None) -> dict[str, int]:
    supplied = {} if value is None else dict(value)
    unknown = set(supplied).difference(DEFAULT_RECURSION_CONFIG)
    if unknown:
        raise ValueError(f"Unknown recursion config fields: {sorted(unknown)}")
    result = dict(DEFAULT_RECURSION_CONFIG)
    result.update(supplied)
    for name, limit in result.items():
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError(f"recursion_config.{name} must be a positive integer")
    return result


def _stored_recursion_config(value: str, *, context: str) -> dict[str, int]:
    loaded = _json_load(value, context=context)
    if not isinstance(loaded, dict):
        raise DatabaseCorruptionError(f"Invalid recursion config stored for {context}")
    try:
        return _normalize_recursion_config(loaded)
    except ValueError as exc:
        raise DatabaseCorruptionError(
            f"Invalid recursion config stored for {context}"
        ) from exc


class SessionStore:
    """SQLite-backed session, transcript, checkpoint, and compaction store.

    API credentials are intentionally absent from this interface and schema.
    Passwords/API keys should only be used by the caller to construct live
    clients, never added to session metadata.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")

        supplied_path = DEFAULT_DATABASE_PATH if path is None else path
        self.path = Path(supplied_path).expanduser() if str(supplied_path) != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._connection = sqlite3.connect(
                str(self.path),
                isolation_level=None,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_schema()
        except SessionStoreError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise DatabaseCorruptionError(
                f"Could not open session database {self.path}: {exc}"
            ) from exc

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None  # type: ignore[assignment]

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for diagnostics/tests, not application writes."""

        if self._connection is None:
            raise SessionStoreError("SessionStore is closed")
        return self._connection

    def _initialize_schema(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Session database schema v{version} is newer than supported v{SCHEMA_VERSION}"
            )

        if version == 0:
            existing = {
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing:
                raise SchemaVersionError(
                    "Session database has tables but no recognized schema version"
                )
            self._create_schema()
            return

        if version == 1:
            self._validate_schema(version=1)
            self._migrate_v1_to_v2()
            return

        if version != SCHEMA_VERSION:
            raise SchemaVersionError(f"Unsupported session database schema v{version}")
        self._validate_schema(version=SCHEMA_VERSION)

    def _create_schema(self) -> None:
        statements = (
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('collecting', 'initializing', 'running',
                               'interrupted', 'completed', 'error')
                ),
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                reasoning_json TEXT NOT NULL,
                compact_config_json TEXT NOT NULL,
                recursion_config_json TEXT NOT NULL,
                cryosparc_url TEXT,
                cryosparc_email TEXT,
                project_uid TEXT,
                workspace_uid TEXT,
                manifest_json TEXT NOT NULL,
                last_error TEXT
            )
            """,
            """
            CREATE TABLE conversation_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                sequence INTEGER NOT NULL,
                origin TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (session_id, sequence)
            )
            """,
            """
            CREATE INDEX conversation_items_session_sequence
                ON conversation_items(session_id, sequence)
            """,
            """
            CREATE UNIQUE INDEX conversation_items_session_id_id
                ON conversation_items(session_id, id)
            """,
            """
            CREATE TRIGGER conversation_items_are_immutable_on_update
            BEFORE UPDATE ON conversation_items
            BEGIN
                SELECT RAISE(ABORT, 'conversation_items are append-only');
            END
            """,
            """
            CREATE TRIGGER conversation_items_are_immutable_on_delete
            BEFORE DELETE ON conversation_items
            BEGIN
                SELECT RAISE(ABORT, 'conversation_items are append-only');
            END
            """,
            """
            CREATE TABLE checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                ordinal INTEGER NOT NULL,
                active_item_ids_json TEXT NOT NULL,
                last_summary_item_id INTEGER REFERENCES conversation_items(id),
                durable_state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (session_id, ordinal)
            )
            """,
            """
            CREATE INDEX checkpoints_session_ordinal
                ON checkpoints(session_id, ordinal)
            """,
            """
            CREATE TABLE compactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                ordinal INTEGER NOT NULL,
                reason TEXT NOT NULL,
                summary_item_id INTEGER NOT NULL REFERENCES conversation_items(id),
                before_active_item_ids_json TEXT NOT NULL,
                after_active_item_ids_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (session_id, ordinal)
            )
            """,
            """
            CREATE INDEX compactions_session_ordinal
                ON compactions(session_id, ordinal)
            """,
            *self._recursive_schema_statements(),
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                self.connection.execute(statement)
            self._raise_on_foreign_key_violations()
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
        self._validate_schema(version=SCHEMA_VERSION)

    @staticmethod
    def _recursive_schema_statements() -> tuple[str, ...]:
        return (
            """
            CREATE TABLE recursive_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                root_turn_item_id INTEGER NOT NULL,
                root_function_call_item_id INTEGER NOT NULL,
                root_function_call_id TEXT NOT NULL,
                parent_call_id INTEGER,
                invoking_call_id TEXT NOT NULL,
                depth INTEGER NOT NULL CHECK (depth >= 1),
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                status TEXT NOT NULL CHECK (
                    status IN ('running', 'completed', 'failed',
                               'interrupted', 'budget_exhausted')
                ),
                active_item_ids_json TEXT NOT NULL,
                final_output_json TEXT,
                metrics_json TEXT NOT NULL,
                error_json TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE (session_id, id),
                UNIQUE (session_id, id, root_turn_item_id),
                UNIQUE (
                    session_id, id, root_turn_item_id,
                    root_function_call_item_id, root_function_call_id
                ),
                UNIQUE (session_id, root_turn_item_id, ordinal),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (session_id, root_turn_item_id)
                    REFERENCES conversation_items(session_id, id),
                FOREIGN KEY (session_id, root_function_call_item_id)
                    REFERENCES conversation_items(session_id, id),
                FOREIGN KEY (
                    session_id, parent_call_id, root_turn_item_id,
                    root_function_call_item_id, root_function_call_id
                ) REFERENCES recursive_calls(
                    session_id, id, root_turn_item_id,
                    root_function_call_item_id, root_function_call_id
                )
            )
            """,
            """
            CREATE INDEX recursive_calls_session_root_ordinal
                ON recursive_calls(session_id, root_turn_item_id, ordinal)
            """,
            """
            CREATE INDEX recursive_calls_parent
                ON recursive_calls(parent_call_id)
            """,
            """
            CREATE TABLE recursive_model_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                call_id INTEGER NOT NULL,
                root_turn_item_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                aggregate_ordinal INTEGER NOT NULL CHECK (aggregate_ordinal >= 0),
                purpose TEXT NOT NULL CHECK (purpose IN ('agent', 'compaction')),
                status TEXT NOT NULL CHECK (
                    status IN ('reserved', 'completed', 'failed', 'interrupted')
                ),
                active_item_ids_json TEXT NOT NULL,
                response_item_ids_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                error_json TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE (session_id, id),
                UNIQUE (session_id, call_id, id),
                UNIQUE (call_id, ordinal),
                UNIQUE (session_id, root_turn_item_id, aggregate_ordinal),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (session_id, root_turn_item_id)
                    REFERENCES conversation_items(session_id, id),
                FOREIGN KEY (session_id, call_id, root_turn_item_id)
                    REFERENCES recursive_calls(session_id, id, root_turn_item_id)
            )
            """,
            """
            CREATE INDEX recursive_model_turns_call_ordinal
                ON recursive_model_turns(call_id, ordinal)
            """,
            """
            CREATE INDEX recursive_model_turns_session_root_aggregate
                ON recursive_model_turns(
                    session_id, root_turn_item_id, aggregate_ordinal
                )
            """,
            """
            CREATE TABLE recursive_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                call_id INTEGER NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                model_turn_id INTEGER,
                origin TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (session_id, id),
                UNIQUE (call_id, sequence),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (session_id, call_id)
                    REFERENCES recursive_calls(session_id, id),
                FOREIGN KEY (session_id, call_id, model_turn_id)
                    REFERENCES recursive_model_turns(session_id, call_id, id)
            )
            """,
            """
            CREATE INDEX recursive_items_call_sequence
                ON recursive_items(call_id, sequence)
            """,
            """
            CREATE TRIGGER recursive_items_are_immutable_on_update
            BEFORE UPDATE ON recursive_items
            BEGIN
                SELECT RAISE(ABORT, 'recursive_items are append-only');
            END
            """,
            """
            CREATE TRIGGER recursive_items_are_immutable_on_delete
            BEFORE DELETE ON recursive_items
            BEGIN
                SELECT RAISE(ABORT, 'recursive_items are append-only');
            END
            """,
        )

    def _migrate_v1_to_v2(self) -> None:
        default_json = _json_dump(DEFAULT_RECURSION_CONFIG).replace("'", "''")
        statements = (
            "ALTER TABLE sessions ADD COLUMN recursion_config_json "
            f"TEXT NOT NULL DEFAULT '{default_json}'",
            "CREATE UNIQUE INDEX conversation_items_session_id_id "
            "ON conversation_items(session_id, id)",
            *self._recursive_schema_statements(),
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                self.connection.execute(statement)
            self._raise_on_foreign_key_violations()
            # The version is deliberately the final migration write.  A failure
            # at any preceding stage therefore leaves a clean, reopenable v1 DB.
            self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
        self._validate_schema(version=SCHEMA_VERSION)

    def _raise_on_foreign_key_violations(self) -> None:
        violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            details = [tuple(row) for row in violations]
            raise SchemaVersionError(
                f"Session database has foreign-key violations: {details}"
            )

    def _validate_schema(self, *, version: int) -> None:
        v1_expected = {
            "sessions": {
                "id", "created_at", "updated_at", "status", "provider", "model",
                "reasoning_json", "compact_config_json", "cryosparc_url",
                "cryosparc_email", "project_uid", "workspace_uid", "manifest_json",
                "last_error",
            },
            "conversation_items": {
                "id", "session_id", "sequence", "origin", "payload_json", "created_at",
            },
            "checkpoints": {
                "id", "session_id", "ordinal", "active_item_ids_json",
                "last_summary_item_id", "durable_state_json", "created_at",
            },
            "compactions": {
                "id", "session_id", "ordinal", "reason", "summary_item_id",
                "before_active_item_ids_json", "after_active_item_ids_json",
                "metrics_json", "created_at",
            },
        }
        expected = {table: set(columns) for table, columns in v1_expected.items()}
        if version == 2:
            expected["sessions"].add("recursion_config_json")
            expected.update(
                {
                    "recursive_calls": {
                        "id", "session_id", "root_turn_item_id",
                        "root_function_call_item_id", "root_function_call_id",
                        "parent_call_id", "invoking_call_id", "depth", "ordinal",
                        "status", "active_item_ids_json", "final_output_json",
                        "metrics_json", "error_json", "started_at", "updated_at",
                        "completed_at",
                    },
                    "recursive_model_turns": {
                        "id", "session_id", "call_id", "root_turn_item_id",
                        "ordinal", "aggregate_ordinal", "purpose", "status",
                        "active_item_ids_json", "response_item_ids_json",
                        "metrics_json", "error_json", "started_at", "completed_at",
                    },
                    "recursive_items": {
                        "id", "session_id", "call_id", "sequence",
                        "model_turn_id", "origin", "payload_json", "created_at",
                    },
                }
            )

        actual_tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual_tables != set(expected):
            raise SchemaVersionError(
                f"Session database schema v{version} has invalid tables"
            )
        for table, columns in expected.items():
            actual = {
                row[1]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            if actual != columns:
                raise SchemaVersionError(
                    f"Session database schema v{version} has invalid table {table!r}"
                )
        required_triggers = {
            "conversation_items_are_immutable_on_update",
            "conversation_items_are_immutable_on_delete",
        }
        if version == 2:
            required_triggers.update(
                {
                    "recursive_items_are_immutable_on_update",
                    "recursive_items_are_immutable_on_delete",
                }
            )
        actual_triggers = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        if not required_triggers.issubset(actual_triggers):
            raise SchemaVersionError(
                f"Session database schema v{version} is missing append-only guards"
            )
        self._raise_on_foreign_key_violations()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _require_session(self, connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"Unknown session ID: {session_id}")
        return row

    def create_session(
        self,
        *,
        provider: str,
        model: str,
        reasoning: Any = None,
        compact_config: Any = None,
        recursion_config: Mapping[str, Any] | None = None,
        cryosparc_url: str | None = None,
        cryosparc_email: str | None = None,
        manifest: Any = None,
        status: str = "collecting",
    ) -> str:
        """Create a session and return its 32-character UUID4 hex ID."""

        self._validate_status(status)
        if not provider:
            raise ValueError("provider is required")
        if not model:
            raise ValueError("model is required")

        # Serialize before opening a transaction so invalid values cannot leave
        # a partial session row.
        reasoning_json = _json_dump(reasoning)
        compact_config_json = _json_dump({} if compact_config is None else compact_config)
        recursion_config_json = _json_dump(_normalize_recursion_config(recursion_config))
        manifest_json = _json_dump({} if manifest is None else manifest)
        session_id = uuid.uuid4().hex
        now = _utcnow()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, created_at, updated_at, status, provider, model,
                    reasoning_json, compact_config_json, recursion_config_json, cryosparc_url,
                    cryosparc_email, project_uid, workspace_uid, manifest_json,
                    last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
                """,
                (
                    session_id,
                    now,
                    now,
                    status,
                    provider,
                    model,
                    reasoning_json,
                    compact_config_json,
                    recursion_config_json,
                    cryosparc_url,
                    cryosparc_email,
                    manifest_json,
                ),
            )
        return session_id

    def get_session(self, session_id: str) -> SessionRecord:
        row = self._require_session(self.connection, session_id)
        return self._session_from_row(row)

    def update_manifest(self, session_id: str, manifest: Any) -> None:
        manifest_json = _json_dump(manifest)
        with self._transaction() as connection:
            self._require_session(connection, session_id)
            connection.execute(
                "UPDATE sessions SET manifest_json = ?, updated_at = ? WHERE id = ?",
                (manifest_json, _utcnow(), session_id),
            )

    def update_scope(
        self,
        session_id: str,
        *,
        project_uid: str | None = None,
        workspace_uid: str | None = None,
    ) -> None:
        """Persist newly created CryoSPARC scope without storing credentials."""

        if project_uid is None and workspace_uid is None:
            raise ValueError("At least one scope UID must be supplied")
        if project_uid is not None and not project_uid:
            raise ValueError("project_uid cannot be empty")
        if workspace_uid is not None and not workspace_uid:
            raise ValueError("workspace_uid cannot be empty")

        with self._transaction() as connection:
            row = self._require_session(connection, session_id)
            resulting_project = project_uid if project_uid is not None else row["project_uid"]
            if workspace_uid is not None and resulting_project is None:
                raise ValueError("A project UID must be stored before its workspace UID")

            assignments: list[str] = []
            values: list[Any] = []
            if project_uid is not None:
                assignments.append("project_uid = ?")
                values.append(project_uid)
            if workspace_uid is not None:
                assignments.append("workspace_uid = ?")
                values.append(workspace_uid)
            assignments.append("updated_at = ?")
            values.extend((_utcnow(), session_id))
            connection.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def set_status(
        self,
        session_id: str,
        status: str,
        *,
        last_error: str | None = None,
    ) -> None:
        self._validate_status(status)
        with self._transaction() as connection:
            self._require_session(connection, session_id)
            connection.execute(
                "UPDATE sessions SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, last_error, _utcnow(), session_id),
            )

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in SESSION_STATUSES:
            allowed = ", ".join(sorted(SESSION_STATUSES))
            raise ValueError(f"Invalid session status {status!r}; expected one of: {allowed}")

    def append_items(
        self,
        session_id: str,
        origin: str,
        items: Iterable[Any],
    ) -> list[int]:
        payload_jsons = self._prepare_items(origin, items)
        if not payload_jsons:
            self.get_session(session_id)
            return []
        with self._transaction() as connection:
            return self._append_items_tx(connection, session_id, origin, payload_jsons)

    def _prepare_items(self, origin: str, items: Iterable[Any]) -> list[str]:
        if not origin:
            raise ValueError("origin is required")
        return [_json_dump(item) for item in items]

    def _append_items_tx(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        origin: str,
        payload_jsons: Sequence[str],
    ) -> list[int]:
        self._require_session(connection, session_id)
        next_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM conversation_items WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        now = _utcnow()
        item_ids: list[int] = []
        for offset, payload_json in enumerate(payload_jsons):
            cursor = connection.execute(
                """
                INSERT INTO conversation_items (
                    session_id, sequence, origin, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, next_sequence + offset, origin, payload_json, now),
            )
            item_ids.append(int(cursor.lastrowid))
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
        return item_ids

    def save_checkpoint(
        self,
        session_id: str,
        active_item_ids: Sequence[int],
        *,
        last_summary_item_id: int | None = None,
        durable_state: Any = None,
    ) -> int:
        active_ids = list(active_item_ids)
        durable_state_json = _json_dump({} if durable_state is None else durable_state)
        with self._transaction() as connection:
            return self._save_checkpoint_tx(
                connection,
                session_id,
                active_ids,
                last_summary_item_id,
                durable_state_json,
            )

    def append_items_and_checkpoint(
        self,
        session_id: str,
        origin: str,
        items: Iterable[Any],
        *,
        active_item_ids: Sequence[int] | None = None,
        new_item_position: int | None = None,
        last_summary_item_id: int | None = None,
        last_summary_new_item_index: int | None = None,
        durable_state: Any = None,
    ) -> tuple[list[int], int]:
        """Atomically append transcript items and advance the active checkpoint.

        ``active_item_ids`` describes the existing active items.  Newly appended
        IDs are inserted at ``new_item_position`` (or appended by default).  A
        compaction summary can therefore be placed after a developer item with
        ``new_item_position=1``.  ``last_summary_new_item_index`` selects one of
        the just-appended items without requiring its database ID in advance.
        """

        if last_summary_item_id is not None and last_summary_new_item_index is not None:
            raise ValueError(
                "Specify last_summary_item_id or last_summary_new_item_index, not both"
            )
        payload_jsons = self._prepare_items(origin, items)
        durable_state_json = _json_dump({} if durable_state is None else durable_state)

        with self._transaction() as connection:
            if active_item_ids is None:
                latest = self._latest_checkpoint_tx(connection, session_id)
                existing_active = [] if latest is None else list(latest.active_item_ids)
            else:
                existing_active = list(active_item_ids)

            new_ids = self._append_items_tx(
                connection, session_id, origin, payload_jsons
            )
            position = len(existing_active) if new_item_position is None else new_item_position
            if not 0 <= position <= len(existing_active):
                raise ValueError("new_item_position is outside the active item list")
            checkpoint_ids = (
                existing_active[:position] + new_ids + existing_active[position:]
            )

            summary_id = last_summary_item_id
            if last_summary_new_item_index is not None:
                try:
                    summary_id = new_ids[last_summary_new_item_index]
                except IndexError as exc:
                    raise ValueError(
                        "last_summary_new_item_index does not select an appended item"
                    ) from exc

            checkpoint_id = self._save_checkpoint_tx(
                connection,
                session_id,
                checkpoint_ids,
                summary_id,
                durable_state_json,
            )
            return new_ids, checkpoint_id

    def append_compaction_and_checkpoint(
        self,
        session_id: str,
        summary_item: Any,
        *,
        reason: str,
        before_active_item_ids: Sequence[int],
        retained_active_item_ids: Sequence[int],
        summary_position: int,
        metrics: Any = None,
        durable_state: Any = None,
    ) -> tuple[int, int, int, list[int]]:
        """Atomically append a summary, checkpoint it, and record compaction metadata.

        ``retained_active_item_ids`` must be an order-preserving subset of the
        pre-compaction context.  The new summary is inserted at
        ``summary_position`` in that retained context.  Returning all generated
        IDs lets the caller update its in-memory mirrors only after the complete
        database transaction commits.
        """

        if not reason:
            raise ValueError("reason is required")
        before_ids = list(before_active_item_ids)
        retained_ids = list(retained_active_item_ids)
        if not 0 <= summary_position <= len(retained_ids):
            raise ValueError("summary_position is outside the retained active item list")

        payload_jsons = self._prepare_items("compaction", [summary_item])
        metrics_json = _json_dump({} if metrics is None else metrics)
        durable_state_json = _json_dump({} if durable_state is None else durable_state)

        with self._transaction() as connection:
            self._require_session(connection, session_id)
            self._validate_item_ids(
                connection,
                session_id,
                before_ids,
                label="pre-compaction context",
            )
            self._validate_item_ids(
                connection,
                session_id,
                retained_ids,
                label="retained compaction context",
            )
            positions = {item_id: index for index, item_id in enumerate(before_ids)}
            try:
                retained_positions = [positions[item_id] for item_id in retained_ids]
            except KeyError as exc:
                raise ValueError(
                    "retained compaction context must be a subset of the pre-compaction context"
                ) from exc
            if retained_positions != sorted(retained_positions):
                raise ValueError(
                    "retained compaction context must preserve pre-compaction order"
                )

            summary_ids = self._append_items_tx(
                connection,
                session_id,
                "compaction",
                payload_jsons,
            )
            summary_item_id = summary_ids[0]
            after_ids = (
                retained_ids[:summary_position]
                + [summary_item_id]
                + retained_ids[summary_position:]
            )
            checkpoint_id = self._save_checkpoint_tx(
                connection,
                session_id,
                after_ids,
                summary_item_id,
                durable_state_json,
            )

            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM compactions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            now = _utcnow()
            cursor = connection.execute(
                """
                INSERT INTO compactions (
                    session_id, ordinal, reason, summary_item_id,
                    before_active_item_ids_json, after_active_item_ids_json,
                    metrics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    ordinal,
                    reason,
                    summary_item_id,
                    _json_dump(before_ids),
                    _json_dump(after_ids),
                    metrics_json,
                    now,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
            return summary_item_id, checkpoint_id, int(cursor.lastrowid), after_ids

    def _save_checkpoint_tx(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        active_item_ids: Sequence[int],
        last_summary_item_id: int | None,
        durable_state_json: str,
    ) -> int:
        self._require_session(connection, session_id)
        self._validate_item_ids(
            connection,
            session_id,
            active_item_ids,
            label="active checkpoint",
        )
        if last_summary_item_id is not None:
            self._validate_item_ids(
                connection,
                session_id,
                [last_summary_item_id],
                label="last summary",
            )
            if last_summary_item_id not in active_item_ids:
                raise ValueError("last_summary_item_id must be present in active_item_ids")

        ordinal = int(
            connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        now = _utcnow()
        cursor = connection.execute(
            """
            INSERT INTO checkpoints (
                session_id, ordinal, active_item_ids_json,
                last_summary_item_id, durable_state_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                ordinal,
                _json_dump(list(active_item_ids)),
                last_summary_item_id,
                durable_state_json,
                now,
            ),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
        return int(cursor.lastrowid)

    def _validate_item_ids(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        item_ids: Sequence[int],
        *,
        label: str,
    ) -> None:
        if not all(
            isinstance(item_id, int) and not isinstance(item_id, bool)
            for item_id in item_ids
        ):
            raise ValueError(f"{label} item IDs must be integers")
        if len(set(item_ids)) != len(item_ids):
            raise ValueError(f"{label} contains duplicate item IDs")
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        rows = connection.execute(
            f"SELECT id FROM conversation_items WHERE session_id = ? AND id IN ({placeholders})",
            (session_id, *item_ids),
        ).fetchall()
        found = {int(row[0]) for row in rows}
        missing = [item_id for item_id in item_ids if item_id not in found]
        if missing:
            raise ValueError(
                f"{label} references items outside session {session_id}: {missing}"
            )

    def record_compaction(
        self,
        session_id: str,
        *,
        reason: str,
        summary_item_id: int,
        before_active_item_ids: Sequence[int],
        after_active_item_ids: Sequence[int],
        metrics: Any = None,
    ) -> int:
        if not reason:
            raise ValueError("reason is required")
        before_ids = list(before_active_item_ids)
        after_ids = list(after_active_item_ids)
        metrics_json = _json_dump({} if metrics is None else metrics)

        with self._transaction() as connection:
            self._require_session(connection, session_id)
            self._validate_item_ids(
                connection, session_id, before_ids, label="pre-compaction context"
            )
            self._validate_item_ids(
                connection, session_id, after_ids, label="post-compaction context"
            )
            self._validate_item_ids(
                connection, session_id, [summary_item_id], label="compaction summary"
            )
            if summary_item_id not in after_ids:
                raise ValueError(
                    "summary_item_id must be present in after_active_item_ids"
                )

            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM compactions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            now = _utcnow()
            cursor = connection.execute(
                """
                INSERT INTO compactions (
                    session_id, ordinal, reason, summary_item_id,
                    before_active_item_ids_json, after_active_item_ids_json,
                    metrics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    ordinal,
                    reason,
                    summary_item_id,
                    _json_dump(before_ids),
                    _json_dump(after_ids),
                    metrics_json,
                    now,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
            return int(cursor.lastrowid)

    def create_recursive_call(
        self,
        session_id: str,
        *,
        invoking_call_id: str,
        bootstrap_items: Iterable[Any],
        parent_call_id: int | None = None,
        root_turn_item_id: int | None = None,
        root_function_call_item_id: int | None = None,
        root_function_call_id: str | None = None,
    ) -> RecursiveCallRecord:
        """Atomically create a recursive branch and its trusted bootstrap.

        Root linkage is supplied only for a top-level child.  Descendants derive
        it, their depth, and their per-root ordinal from the persisted parent so
        callers cannot accidentally move a branch into another root tree.
        """

        if not invoking_call_id:
            raise ValueError("invoking_call_id is required")
        bootstrap_jsons = self._prepare_items("bootstrap", bootstrap_items)

        with self._transaction() as connection:
            session_row = self._require_session(connection, session_id)
            config = _stored_recursion_config(
                session_row["recursion_config_json"],
                context=f"session {session_id} recursion config",
            )

            if parent_call_id is None:
                if root_turn_item_id is None:
                    raise ValueError("root_turn_item_id is required for a top-level call")
                if root_function_call_item_id is None:
                    raise ValueError(
                        "root_function_call_item_id is required for a top-level call"
                    )
                if not root_function_call_id:
                    raise ValueError(
                        "root_function_call_id is required for a top-level call"
                    )
                self._validate_item_ids(
                    connection,
                    session_id,
                    [root_turn_item_id, root_function_call_item_id],
                    label="recursive root linkage",
                )
                resolved_root_turn_id = root_turn_item_id
                resolved_root_function_item_id = root_function_call_item_id
                resolved_root_function_id = root_function_call_id
                depth = 1
            else:
                parent_row = self._require_recursive_call(connection, parent_call_id)
                if parent_row["session_id"] != session_id:
                    raise ValueError("parent_call_id belongs to another session")
                if parent_row["status"] != "running":
                    raise ValueError("parent recursive call is not running")
                resolved_root_turn_id = int(parent_row["root_turn_item_id"])
                resolved_root_function_item_id = int(
                    parent_row["root_function_call_item_id"]
                )
                resolved_root_function_id = parent_row["root_function_call_id"]
                supplied_linkage = (
                    ("root_turn_item_id", root_turn_item_id, resolved_root_turn_id),
                    (
                        "root_function_call_item_id",
                        root_function_call_item_id,
                        resolved_root_function_item_id,
                    ),
                    (
                        "root_function_call_id",
                        root_function_call_id,
                        resolved_root_function_id,
                    ),
                )
                for name, supplied, expected in supplied_linkage:
                    if supplied is not None and supplied != expected:
                        raise ValueError(f"{name} does not match the parent branch")
                depth = int(parent_row["depth"]) + 1

            if depth > config["max_depth"]:
                raise RecursiveLimitError(
                    f"Recursive depth {depth} exceeds max_depth={config['max_depth']}"
                )
            call_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM recursive_calls
                    WHERE session_id = ? AND root_turn_item_id = ?
                    """,
                    (session_id, resolved_root_turn_id),
                ).fetchone()[0]
            )
            if call_count >= config["max_calls"]:
                raise RecursiveLimitError(
                    "Recursive call budget exhausted for root turn "
                    f"{resolved_root_turn_id} (max_calls={config['max_calls']})"
                )
            ordinal = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(ordinal), -1) + 1 FROM recursive_calls
                    WHERE session_id = ? AND root_turn_item_id = ?
                    """,
                    (session_id, resolved_root_turn_id),
                ).fetchone()[0]
            )
            now = _utcnow()
            cursor = connection.execute(
                """
                INSERT INTO recursive_calls (
                    session_id, root_turn_item_id, root_function_call_item_id,
                    root_function_call_id, parent_call_id, invoking_call_id,
                    depth, ordinal, status, active_item_ids_json,
                    final_output_json, metrics_json, error_json,
                    started_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', '[]',
                          NULL, '{}', NULL, ?, ?, NULL)
                """,
                (
                    session_id,
                    resolved_root_turn_id,
                    resolved_root_function_item_id,
                    resolved_root_function_id,
                    parent_call_id,
                    invoking_call_id,
                    depth,
                    ordinal,
                    now,
                    now,
                ),
            )
            call_id = int(cursor.lastrowid)
            call_row = self._require_recursive_call(connection, call_id)
            bootstrap_ids = self._append_recursive_items_tx(
                connection,
                call_row,
                "bootstrap",
                bootstrap_jsons,
            )
            now = _utcnow()
            connection.execute(
                """
                UPDATE recursive_calls
                SET active_item_ids_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json_dump(bootstrap_ids), now, call_id),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            result_row = self._require_recursive_call(connection, call_id)
        return self._recursive_call_from_row(result_row)

    def reserve_recursive_model_turn(
        self,
        call_id: int,
        *,
        purpose: str = "agent",
        active_item_ids: Sequence[int] | None = None,
    ) -> RecursiveModelTurnRecord:
        """Reserve and count one inference before making an external request."""

        if purpose not in RECURSIVE_MODEL_TURN_PURPOSES:
            raise ValueError("purpose must be 'agent' or 'compaction'")
        budget_error: str | None = None
        result_row: sqlite3.Row | None = None
        with self._transaction() as connection:
            call_row = self._require_recursive_call(connection, call_id)
            if call_row["status"] != "running":
                raise ValueError("recursive call is not running")
            session_id = call_row["session_id"]
            session_row = self._require_session(connection, session_id)
            config = _stored_recursion_config(
                session_row["recursion_config_json"],
                context=f"session {session_id} recursion config",
            )
            root_turn_item_id = int(call_row["root_turn_item_id"])
            turn_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM recursive_model_turns
                    WHERE session_id = ? AND root_turn_item_id = ?
                    """,
                    (session_id, root_turn_item_id),
                ).fetchone()[0]
            )
            if turn_count >= config["max_model_turns"]:
                budget_error = (
                    "Recursive model-turn budget exhausted for root turn "
                    f"{root_turn_item_id} "
                    f"(max_model_turns={config['max_model_turns']})"
                )
                error_value = {
                    "type": "model_turn_budget_exhausted",
                    "limit": config["max_model_turns"],
                    "root_turn_item_id": root_turn_item_id,
                }
                budget_ids = self._append_recursive_items_tx(
                    connection,
                    call_row,
                    "budget",
                    [_json_dump(error_value)],
                )
                current_active = self._ids_from_json(
                    call_row["active_item_ids_json"],
                    context=f"recursive call {call_id} active IDs",
                )
                now = _utcnow()
                connection.execute(
                    """
                    UPDATE recursive_calls
                    SET status = 'budget_exhausted', active_item_ids_json = ?,
                        error_json = ?, updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        _json_dump([*current_active, *budget_ids]),
                        _json_dump(error_value),
                        now,
                        now,
                        call_id,
                    ),
                )
            else:
                if active_item_ids is None:
                    active_ids = self._ids_from_json(
                        call_row["active_item_ids_json"],
                        context=f"recursive call {call_id} active IDs",
                    )
                else:
                    active_ids = list(active_item_ids)
                self._validate_recursive_item_ids(
                    connection,
                    session_id,
                    call_id,
                    active_ids,
                    label="recursive model input",
                )
                ordinal = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(ordinal), -1) + 1
                        FROM recursive_model_turns WHERE call_id = ?
                        """,
                        (call_id,),
                    ).fetchone()[0]
                )
                aggregate_ordinal = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(aggregate_ordinal), -1) + 1
                        FROM recursive_model_turns
                        WHERE session_id = ? AND root_turn_item_id = ?
                        """,
                        (session_id, root_turn_item_id),
                    ).fetchone()[0]
                )
                now = _utcnow()
                cursor = connection.execute(
                    """
                    INSERT INTO recursive_model_turns (
                        session_id, call_id, root_turn_item_id, ordinal,
                        aggregate_ordinal, purpose, status,
                        active_item_ids_json, response_item_ids_json,
                        metrics_json, error_json, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, '[]', '{}',
                              NULL, ?, NULL)
                    """,
                    (
                        session_id,
                        call_id,
                        root_turn_item_id,
                        ordinal,
                        aggregate_ordinal,
                        purpose,
                        _json_dump(active_ids),
                        now,
                    ),
                )
                result_row = self._require_recursive_model_turn(
                    connection, int(cursor.lastrowid)
                )
            now = _utcnow()
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        if budget_error is not None:
            raise RecursiveLimitError(budget_error)
        assert result_row is not None
        return self._recursive_model_turn_from_row(result_row)

    def complete_recursive_model_turn(
        self,
        turn_id: int,
        response_items: Iterable[Any],
        *,
        retained_active_item_ids: Sequence[int] | None = None,
        new_item_position: int | None = None,
        preserve_call_active_context: bool = False,
        metrics: Any = None,
    ) -> tuple[RecursiveModelTurnRecord, list[int]]:
        """Atomically append a model response and finish its reservation."""

        response_jsons = self._prepare_items("model", response_items)
        metrics_json = _json_dump({} if metrics is None else metrics)
        if preserve_call_active_context and (
            retained_active_item_ids is not None or new_item_position is not None
        ):
            raise ValueError(
                "preserve_call_active_context cannot be combined with retained IDs "
                "or new_item_position"
            )
        with self._transaction() as connection:
            turn_row = self._require_recursive_model_turn(connection, turn_id)
            if turn_row["status"] != "reserved":
                raise ValueError("recursive model turn is not reserved")
            call_id = int(turn_row["call_id"])
            call_row = self._require_recursive_call(connection, call_id)
            if call_row["status"] != "running":
                raise ValueError("recursive call is not running")
            before_ids = self._ids_from_json(
                turn_row["active_item_ids_json"],
                context=f"recursive model turn {turn_id} active IDs",
            )
            if preserve_call_active_context:
                retained_ids = self._ids_from_json(
                    call_row["active_item_ids_json"],
                    context=f"recursive call {call_id} active IDs",
                )
                position = len(retained_ids)
            else:
                retained_ids = (
                    before_ids
                    if retained_active_item_ids is None
                    else list(retained_active_item_ids)
                )
                self._validate_ordered_subset(
                    before_ids,
                    retained_ids,
                    label="retained recursive model context",
                )
                self._validate_recursive_item_ids(
                    connection,
                    call_row["session_id"],
                    call_id,
                    retained_ids,
                    label="retained recursive model context",
                )
                position = (
                    len(retained_ids)
                    if new_item_position is None
                    else new_item_position
                )
                if not 0 <= position <= len(retained_ids):
                    raise ValueError(
                        "new_item_position is outside the retained active items"
                    )
            origin = "compaction" if turn_row["purpose"] == "compaction" else "model"
            response_ids = self._append_recursive_items_tx(
                connection,
                call_row,
                origin,
                response_jsons,
                model_turn_id=turn_id,
            )
            resulting_active_ids = (
                retained_ids
                if preserve_call_active_context
                else retained_ids[:position] + response_ids + retained_ids[position:]
            )
            now = _utcnow()
            connection.execute(
                """
                UPDATE recursive_model_turns
                SET status = 'completed', response_item_ids_json = ?,
                    metrics_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (_json_dump(response_ids), metrics_json, now, turn_id),
            )
            connection.execute(
                """
                UPDATE recursive_calls
                SET active_item_ids_json = ?, updated_at = ? WHERE id = ?
                """,
                (_json_dump(resulting_active_ids), now, call_id),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, call_row["session_id"]),
            )
            result_row = self._require_recursive_model_turn(connection, turn_id)
        return self._recursive_model_turn_from_row(result_row), resulting_active_ids

    def append_recursive_trace_items(
        self,
        call_id: int,
        items: Iterable[Any],
        *,
        origin: str,
        model_turn_id: int | None = None,
    ) -> list[int]:
        """Append audit/protocol items without changing the branch active context."""

        payload_jsons = self._prepare_items(origin, items)
        with self._transaction() as connection:
            call_row = self._require_recursive_call(connection, call_id)
            if call_row["status"] != "running":
                raise ValueError("recursive call is not running")
            item_ids = self._append_recursive_items_tx(
                connection,
                call_row,
                origin,
                payload_jsons,
                model_turn_id=model_turn_id,
            )
        return item_ids

    def fail_recursive_model_turn(
        self,
        turn_id: int,
        error: Any,
        *,
        status: str = "failed",
        metrics: Any = None,
    ) -> RecursiveModelTurnRecord:
        """Finish a reserved inference without inventing a provider response."""

        if status not in {"failed", "interrupted"}:
            raise ValueError("failed model turn status must be 'failed' or 'interrupted'")
        error_json = _json_dump(error)
        metrics_json = _json_dump({} if metrics is None else metrics)
        with self._transaction() as connection:
            turn_row = self._require_recursive_model_turn(connection, turn_id)
            if turn_row["status"] != "reserved":
                raise ValueError("recursive model turn is not reserved")
            now = _utcnow()
            connection.execute(
                """
                UPDATE recursive_model_turns
                SET status = ?, metrics_json = ?, error_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, metrics_json, error_json, now, turn_id),
            )
            call_row = self._require_recursive_call(
                connection, int(turn_row["call_id"])
            )
            connection.execute(
                "UPDATE recursive_calls SET updated_at = ? WHERE id = ?",
                (now, int(call_row["id"])),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, call_row["session_id"]),
            )
            result_row = self._require_recursive_model_turn(connection, turn_id)
        return self._recursive_model_turn_from_row(result_row)

    def append_recursive_tool_bundle(
        self,
        call_id: int,
        items: Iterable[Any],
        *,
        model_turn_id: int | None = None,
        retained_active_item_ids: Sequence[int] | None = None,
        new_item_position: int | None = None,
        origin: str = "tool",
    ) -> list[int]:
        """Append one complete child tool-result bundle transactionally."""

        payload_jsons = self._prepare_items(origin, items)
        with self._transaction() as connection:
            item_ids, _resulting_ids = self._append_recursive_bundle_tx(
                connection,
                call_id,
                payload_jsons,
                origin=origin,
                model_turn_id=model_turn_id,
                retained_active_item_ids=retained_active_item_ids,
                new_item_position=new_item_position,
            )
        return item_ids

    def append_recursive_tool_bundle_and_checkpoint(
        self,
        call_id: int,
        items: Iterable[Any],
        *,
        root_active_item_ids: Sequence[int],
        model_turn_id: int | None = None,
        retained_active_item_ids: Sequence[int] | None = None,
        new_item_position: int | None = None,
        origin: str = "tool",
        root_last_summary_item_id: int | None = None,
        durable_state: Any = None,
    ) -> tuple[list[int], int]:
        """Append a child tool bundle and checkpoint the unchanged root context."""

        payload_jsons = self._prepare_items(origin, items)
        durable_state_json = _json_dump({} if durable_state is None else durable_state)
        with self._transaction() as connection:
            call_row = self._require_recursive_call(connection, call_id)
            item_ids, _resulting_ids = self._append_recursive_bundle_tx(
                connection,
                call_id,
                payload_jsons,
                origin=origin,
                model_turn_id=model_turn_id,
                retained_active_item_ids=retained_active_item_ids,
                new_item_position=new_item_position,
            )
            checkpoint_id = self._save_checkpoint_tx(
                connection,
                call_row["session_id"],
                list(root_active_item_ids),
                root_last_summary_item_id,
                durable_state_json,
            )
        return item_ids, checkpoint_id

    def finalize_recursive_call(
        self,
        call_id: int,
        final_output: Any = None,
        *,
        metrics: Any = None,
        error: Any = None,
        status: str = "completed",
    ) -> RecursiveCallRecord:
        """Mark a branch terminal after all of its provider turns are balanced."""

        if status not in RECURSIVE_CALL_STATUSES.difference({"running"}):
            raise ValueError("final recursive call status is invalid")
        final_output_json = _json_dump(final_output)
        metrics_json = _json_dump({} if metrics is None else metrics)
        error_json = None if error is None else _json_dump(error)
        with self._transaction() as connection:
            call_row = self._require_recursive_call(connection, call_id)
            if call_row["status"] != "running":
                raise ValueError("recursive call is already terminal")
            reserved_rows = connection.execute(
                """
                SELECT id FROM recursive_model_turns
                WHERE call_id = ? AND status = 'reserved' ORDER BY ordinal
                """,
                (call_id,),
            ).fetchall()
            if reserved_rows and status == "completed":
                raise ValueError("cannot complete a call with a reserved model turn")
            now = _utcnow()
            if reserved_rows:
                turn_status = "interrupted" if status == "interrupted" else "failed"
                connection.execute(
                    """
                    UPDATE recursive_model_turns
                    SET status = ?, error_json = ?, completed_at = ?
                    WHERE call_id = ? AND status = 'reserved'
                    """,
                    (turn_status, error_json or _json_dump({"type": status}), now, call_id),
                )
            connection.execute(
                """
                UPDATE recursive_calls
                SET status = ?, final_output_json = ?, metrics_json = ?,
                    error_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    final_output_json,
                    metrics_json,
                    error_json,
                    now,
                    now,
                    call_id,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, call_row["session_id"]),
            )
            result_row = self._require_recursive_call(connection, call_id)
        return self._recursive_call_from_row(result_row)

    def fail_recursive_call(
        self,
        call_id: int,
        error: Any,
        *,
        status: str = "failed",
        metrics: Any = None,
    ) -> RecursiveCallRecord:
        """Convenience wrapper for terminal failed/interrupted branches."""

        if status not in {"failed", "interrupted", "budget_exhausted"}:
            raise ValueError("failed call status is invalid")
        return self.finalize_recursive_call(
            call_id,
            None,
            metrics=metrics,
            error=error,
            status=status,
        )

    def recover_incomplete_recursive_calls(
        self,
        session_id: str,
        *,
        error: Any = None,
        recovery_item: Any = None,
    ) -> list[RecursiveCallRecord]:
        """Interrupt incomplete branches deepest-first without replaying work."""

        recovery_error = (
            {"type": "process_interrupted", "message": "recursive call did not finish"}
            if error is None
            else error
        )
        error_json = _json_dump(recovery_error)
        recovered_rows: list[sqlite3.Row] = []
        with self._transaction() as connection:
            self._require_session(connection, session_id)
            incomplete = connection.execute(
                """
                SELECT * FROM recursive_calls
                WHERE session_id = ? AND status = 'running'
                ORDER BY depth DESC, id DESC
                """,
                (session_id,),
            ).fetchall()
            now = _utcnow()
            for call_row in incomplete:
                call_id = int(call_row["id"])
                payload = (
                    {
                        "type": "recursive_recovery",
                        "call_id": call_id,
                        "status": "interrupted",
                        "error": recovery_error,
                    }
                    if recovery_item is None
                    else recovery_item
                )
                recovery_ids = self._append_recursive_items_tx(
                    connection,
                    call_row,
                    "recovery",
                    [_json_dump(payload)],
                )
                active_ids = self._ids_from_json(
                    call_row["active_item_ids_json"],
                    context=f"recursive call {call_id} active IDs",
                )
                connection.execute(
                    """
                    UPDATE recursive_model_turns
                    SET status = 'interrupted', error_json = ?, completed_at = ?
                    WHERE call_id = ? AND status = 'reserved'
                    """,
                    (error_json, now, call_id),
                )
                connection.execute(
                    """
                    UPDATE recursive_calls
                    SET status = 'interrupted', active_item_ids_json = ?,
                        error_json = ?, updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        _json_dump([*active_ids, *recovery_ids]),
                        error_json,
                        now,
                        now,
                        call_id,
                    ),
                )
                recovered_rows.append(
                    self._require_recursive_call(connection, call_id)
                )
            if incomplete:
                connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
        return [self._recursive_call_from_row(row) for row in recovered_rows]

    def get_recursive_call(self, call_id: int) -> RecursiveCallRecord:
        return self._recursive_call_from_row(
            self._require_recursive_call(self.connection, call_id)
        )

    def get_recursive_model_turn(self, turn_id: int) -> RecursiveModelTurnRecord:
        return self._recursive_model_turn_from_row(
            self._require_recursive_model_turn(self.connection, turn_id)
        )

    def list_recursive_calls(
        self,
        session_id: str,
        *,
        root_turn_item_id: int | None = None,
        limit: int | None = None,
    ) -> list[RecursiveCallRecord]:
        self.get_session(session_id)
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer")
        where = "session_id = ?"
        values: list[Any] = [session_id]
        if root_turn_item_id is not None:
            where += " AND root_turn_item_id = ?"
            values.append(root_turn_item_id)
        query = f"SELECT * FROM recursive_calls WHERE {where} ORDER BY id"
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        rows = self.connection.execute(query, values).fetchall()
        return [self._recursive_call_from_row(row) for row in rows]

    def list_recursive_model_turns(
        self, call_id: int
    ) -> list[RecursiveModelTurnRecord]:
        self.get_recursive_call(call_id)
        rows = self.connection.execute(
            "SELECT * FROM recursive_model_turns WHERE call_id = ? ORDER BY ordinal",
            (call_id,),
        ).fetchall()
        return [self._recursive_model_turn_from_row(row) for row in rows]

    def list_recursive_items(
        self,
        call_id: int,
        *,
        include_descendants: bool = False,
    ) -> list[RecursiveItemRecord]:
        self.get_recursive_call(call_id)
        call_ids = [call_id]
        if include_descendants:
            call_ids.extend(self._descendant_call_ids(self.connection, call_id))
        placeholders = ",".join("?" for _ in call_ids)
        rows = self.connection.execute(
            f"SELECT * FROM recursive_items WHERE call_id IN ({placeholders}) ORDER BY id",
            call_ids,
        ).fetchall()
        return [self._recursive_item_from_row(row) for row in rows]

    def load_recursive_history(
        self,
        call_id: int,
        *,
        include_descendants: bool = False,
    ) -> list[Any]:
        return [
            item.payload
            for item in self.list_recursive_items(
                call_id, include_descendants=include_descendants
            )
        ]

    def load_recursive_call(
        self,
        call_id: int,
        *,
        include_descendants: bool = False,
    ) -> RecursiveCallSnapshot:
        call = self.get_recursive_call(call_id)
        descendant_ids = (
            self._descendant_call_ids(self.connection, call_id)
            if include_descendants
            else []
        )
        descendants = tuple(self.get_recursive_call(item_id) for item_id in descendant_ids)
        turn_call_ids = [call_id, *descendant_ids]
        placeholders = ",".join("?" for _ in turn_call_ids)
        turn_rows = self.connection.execute(
            f"""
            SELECT * FROM recursive_model_turns
            WHERE call_id IN ({placeholders}) ORDER BY id
            """,
            turn_call_ids,
        ).fetchall()
        return RecursiveCallSnapshot(
            call=call,
            items=tuple(
                self.list_recursive_items(
                    call_id, include_descendants=include_descendants
                )
            ),
            model_turns=tuple(
                self._recursive_model_turn_from_row(row) for row in turn_rows
            ),
            descendants=descendants,
        )

    def _append_recursive_bundle_tx(
        self,
        connection: sqlite3.Connection,
        call_id: int,
        payload_jsons: Sequence[str],
        *,
        origin: str,
        model_turn_id: int | None,
        retained_active_item_ids: Sequence[int] | None,
        new_item_position: int | None,
    ) -> tuple[list[int], list[int]]:
        call_row = self._require_recursive_call(connection, call_id)
        if call_row["status"] != "running":
            raise ValueError("recursive call is not running")
        current_ids = self._ids_from_json(
            call_row["active_item_ids_json"],
            context=f"recursive call {call_id} active IDs",
        )
        retained_ids = (
            current_ids
            if retained_active_item_ids is None
            else list(retained_active_item_ids)
        )
        self._validate_ordered_subset(
            current_ids,
            retained_ids,
            label="retained recursive tool context",
        )
        self._validate_recursive_item_ids(
            connection,
            call_row["session_id"],
            call_id,
            retained_ids,
            label="retained recursive tool context",
        )
        position = len(retained_ids) if new_item_position is None else new_item_position
        if not 0 <= position <= len(retained_ids):
            raise ValueError("new_item_position is outside the retained active items")
        item_ids = self._append_recursive_items_tx(
            connection,
            call_row,
            origin,
            payload_jsons,
            model_turn_id=model_turn_id,
        )
        resulting_ids = retained_ids[:position] + item_ids + retained_ids[position:]
        now = _utcnow()
        connection.execute(
            """
            UPDATE recursive_calls
            SET active_item_ids_json = ?, updated_at = ? WHERE id = ?
            """,
            (_json_dump(resulting_ids), now, call_id),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, call_row["session_id"]),
        )
        return item_ids, resulting_ids

    def _append_recursive_items_tx(
        self,
        connection: sqlite3.Connection,
        call_row: sqlite3.Row,
        origin: str,
        payload_jsons: Sequence[str],
        *,
        model_turn_id: int | None = None,
    ) -> list[int]:
        if not origin:
            raise ValueError("recursive item origin is required")
        call_id = int(call_row["id"])
        session_id = call_row["session_id"]
        if model_turn_id is not None:
            turn_row = self._require_recursive_model_turn(connection, model_turn_id)
            if (
                turn_row["session_id"] != session_id
                or int(turn_row["call_id"]) != call_id
            ):
                raise ValueError("model_turn_id belongs to another recursive call")
        next_sequence = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(sequence), -1) + 1
                FROM recursive_items WHERE call_id = ?
                """,
                (call_id,),
            ).fetchone()[0]
        )
        now = _utcnow()
        item_ids: list[int] = []
        for offset, payload_json in enumerate(payload_jsons):
            cursor = connection.execute(
                """
                INSERT INTO recursive_items (
                    session_id, call_id, sequence, model_turn_id,
                    origin, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    call_id,
                    next_sequence + offset,
                    model_turn_id,
                    origin,
                    payload_json,
                    now,
                ),
            )
            item_ids.append(int(cursor.lastrowid))
        connection.execute(
            "UPDATE recursive_calls SET updated_at = ? WHERE id = ?",
            (now, call_id),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        return item_ids

    def _validate_recursive_item_ids(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        call_id: int,
        item_ids: Sequence[int],
        *,
        label: str,
    ) -> None:
        if not all(
            isinstance(item_id, int) and not isinstance(item_id, bool)
            for item_id in item_ids
        ):
            raise ValueError(f"{label} item IDs must be integers")
        if len(set(item_ids)) != len(item_ids):
            raise ValueError(f"{label} contains duplicate item IDs")
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        rows = connection.execute(
            f"""
            SELECT id FROM recursive_items
            WHERE session_id = ? AND call_id = ? AND id IN ({placeholders})
            """,
            (session_id, call_id, *item_ids),
        ).fetchall()
        found = {int(row[0]) for row in rows}
        missing = [item_id for item_id in item_ids if item_id not in found]
        if missing:
            raise ValueError(
                f"{label} references items outside recursive call {call_id}: {missing}"
            )

    @staticmethod
    def _validate_ordered_subset(
        before_ids: Sequence[int],
        retained_ids: Sequence[int],
        *,
        label: str,
    ) -> None:
        positions = {item_id: index for index, item_id in enumerate(before_ids)}
        try:
            retained_positions = [positions[item_id] for item_id in retained_ids]
        except KeyError as exc:
            raise ValueError(f"{label} must be a subset of the active context") from exc
        if retained_positions != sorted(retained_positions):
            raise ValueError(f"{label} must preserve active-context order")
        if len(set(retained_ids)) != len(retained_ids):
            raise ValueError(f"{label} contains duplicate item IDs")

    @staticmethod
    def _ids_from_json(value: str, *, context: str) -> list[int]:
        item_ids = _json_load(value, context=context)
        if (
            not isinstance(item_ids, list)
            or not all(
                isinstance(item_id, int) and not isinstance(item_id, bool)
                for item_id in item_ids
            )
            or len(set(item_ids)) != len(item_ids)
        ):
            raise DatabaseCorruptionError(f"Invalid item IDs stored for {context}")
        return item_ids

    def _require_recursive_call(
        self, connection: sqlite3.Connection, call_id: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM recursive_calls WHERE id = ?", (call_id,)
        ).fetchone()
        if row is None:
            raise RecursiveCallNotFoundError(f"Unknown recursive call ID: {call_id}")
        return row

    def _require_recursive_model_turn(
        self, connection: sqlite3.Connection, turn_id: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM recursive_model_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        if row is None:
            raise RecursiveCallNotFoundError(
                f"Unknown recursive model turn ID: {turn_id}"
            )
        return row

    @staticmethod
    def _descendant_call_ids(
        connection: sqlite3.Connection, call_id: int
    ) -> list[int]:
        rows = connection.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT id FROM recursive_calls WHERE parent_call_id = ?
                UNION ALL
                SELECT child.id FROM recursive_calls AS child
                JOIN descendants AS parent ON child.parent_call_id = parent.id
            )
            SELECT id FROM descendants ORDER BY id
            """,
            (call_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def list_conversation_items(self, session_id: str) -> list[ConversationItemRecord]:
        self.get_session(session_id)
        rows = self.connection.execute(
            "SELECT * FROM conversation_items WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
        return [self._conversation_item_from_row(row) for row in rows]

    def load_conversation(self, session_id: str) -> list[Any]:
        """Load exact transcript JSON values in append order."""

        return [item.payload for item in self.list_conversation_items(session_id)]

    def list_checkpoints(self, session_id: str) -> list[CheckpointRecord]:
        self.get_session(session_id)
        rows = self.connection.execute(
            "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    def list_compactions(self, session_id: str) -> list[CompactionRecord]:
        self.get_session(session_id)
        rows = self.connection.execute(
            "SELECT * FROM compactions WHERE session_id = ? ORDER BY ordinal",
            (session_id,),
        ).fetchall()
        return [self._compaction_from_row(row) for row in rows]

    def load_snapshot(self, session_id: str) -> SessionSnapshot:
        session = self.get_session(session_id)
        conversation_items = tuple(self.list_conversation_items(session_id))
        by_id = {item.id: item for item in conversation_items}
        checkpoint = self._latest_checkpoint_tx(self.connection, session_id)

        active_items: tuple[ConversationItemRecord, ...] = ()
        summary_item: ConversationItemRecord | None = None
        if checkpoint is not None:
            try:
                active_items = tuple(by_id[item_id] for item_id in checkpoint.active_item_ids)
                if checkpoint.last_summary_item_id is not None:
                    summary_item = by_id[checkpoint.last_summary_item_id]
            except KeyError as exc:
                raise DatabaseCorruptionError(
                    f"Checkpoint for session {session_id} references a missing transcript item"
                ) from exc

        return SessionSnapshot(
            session=session,
            conversation_items=conversation_items,
            checkpoint=checkpoint,
            active_items=active_items,
            last_summary_item=summary_item,
            compactions=tuple(self.list_compactions(session_id)),
        )

    def _latest_checkpoint_tx(
        self, connection: sqlite3.Connection, session_id: str
    ) -> CheckpointRecord | None:
        self._require_session(connection, session_id)
        row = connection.execute(
            "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY ordinal DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return None if row is None else self._checkpoint_from_row(row)

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            provider=row["provider"],
            model=row["model"],
            reasoning=_json_load(row["reasoning_json"], context="session reasoning"),
            compact_config=_json_load(
                row["compact_config_json"], context="session compaction config"
            ),
            recursion_config=_stored_recursion_config(
                row["recursion_config_json"], context="session recursion config"
            ),
            cryosparc_url=row["cryosparc_url"],
            cryosparc_email=row["cryosparc_email"],
            project_uid=row["project_uid"],
            workspace_uid=row["workspace_uid"],
            manifest=_json_load(row["manifest_json"], context="session manifest"),
            last_error=row["last_error"],
        )

    @staticmethod
    def _conversation_item_from_row(row: sqlite3.Row) -> ConversationItemRecord:
        return ConversationItemRecord(
            id=int(row["id"]),
            session_id=row["session_id"],
            sequence=int(row["sequence"]),
            origin=row["origin"],
            payload=_json_load(
                row["payload_json"], context=f"conversation item {row['id']}"
            ),
            created_at=row["created_at"],
        )

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> CheckpointRecord:
        active_ids = _json_load(
            row["active_item_ids_json"], context=f"checkpoint {row['id']} active IDs"
        )
        if not isinstance(active_ids, list) or not all(
            isinstance(item_id, int) and not isinstance(item_id, bool)
            for item_id in active_ids
        ):
            raise DatabaseCorruptionError(
                f"Checkpoint {row['id']} has invalid active item IDs"
            )
        return CheckpointRecord(
            id=int(row["id"]),
            session_id=row["session_id"],
            ordinal=int(row["ordinal"]),
            active_item_ids=tuple(active_ids),
            last_summary_item_id=(
                None
                if row["last_summary_item_id"] is None
                else int(row["last_summary_item_id"])
            ),
            durable_state=_json_load(
                row["durable_state_json"], context=f"checkpoint {row['id']} state"
            ),
            created_at=row["created_at"],
        )

    @staticmethod
    def _compaction_from_row(row: sqlite3.Row) -> CompactionRecord:
        before_ids = _json_load(
            row["before_active_item_ids_json"],
            context=f"compaction {row['id']} before IDs",
        )
        after_ids = _json_load(
            row["after_active_item_ids_json"],
            context=f"compaction {row['id']} after IDs",
        )
        for label, item_ids in (("before", before_ids), ("after", after_ids)):
            if not isinstance(item_ids, list) or not all(
                isinstance(item_id, int) and not isinstance(item_id, bool)
                for item_id in item_ids
            ):
                raise DatabaseCorruptionError(
                    f"Compaction {row['id']} has invalid {label} item IDs"
                )
        return CompactionRecord(
            id=int(row["id"]),
            session_id=row["session_id"],
            ordinal=int(row["ordinal"]),
            reason=row["reason"],
            summary_item_id=int(row["summary_item_id"]),
            before_active_item_ids=tuple(before_ids),
            after_active_item_ids=tuple(after_ids),
            metrics=_json_load(
                row["metrics_json"], context=f"compaction {row['id']} metrics"
            ),
            created_at=row["created_at"],
        )

    @classmethod
    def _recursive_call_from_row(cls, row: sqlite3.Row) -> RecursiveCallRecord:
        active_ids = cls._ids_from_json(
            row["active_item_ids_json"],
            context=f"recursive call {row['id']} active IDs",
        )
        return RecursiveCallRecord(
            id=int(row["id"]),
            session_id=row["session_id"],
            root_turn_item_id=int(row["root_turn_item_id"]),
            root_function_call_item_id=int(row["root_function_call_item_id"]),
            root_function_call_id=row["root_function_call_id"],
            parent_call_id=(
                None if row["parent_call_id"] is None else int(row["parent_call_id"])
            ),
            invoking_call_id=row["invoking_call_id"],
            depth=int(row["depth"]),
            ordinal=int(row["ordinal"]),
            status=row["status"],
            active_item_ids=tuple(active_ids),
            final_output=(
                None
                if row["final_output_json"] is None
                else _json_load(
                    row["final_output_json"],
                    context=f"recursive call {row['id']} final output",
                )
            ),
            metrics=_json_load(
                row["metrics_json"], context=f"recursive call {row['id']} metrics"
            ),
            error=(
                None
                if row["error_json"] is None
                else _json_load(
                    row["error_json"],
                    context=f"recursive call {row['id']} error",
                )
            ),
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @classmethod
    def _recursive_model_turn_from_row(
        cls, row: sqlite3.Row
    ) -> RecursiveModelTurnRecord:
        active_ids = cls._ids_from_json(
            row["active_item_ids_json"],
            context=f"recursive model turn {row['id']} active IDs",
        )
        response_ids = cls._ids_from_json(
            row["response_item_ids_json"],
            context=f"recursive model turn {row['id']} response IDs",
        )
        return RecursiveModelTurnRecord(
            id=int(row["id"]),
            session_id=row["session_id"],
            call_id=int(row["call_id"]),
            root_turn_item_id=int(row["root_turn_item_id"]),
            ordinal=int(row["ordinal"]),
            aggregate_ordinal=int(row["aggregate_ordinal"]),
            purpose=row["purpose"],
            status=row["status"],
            active_item_ids=tuple(active_ids),
            response_item_ids=tuple(response_ids),
            metrics=_json_load(
                row["metrics_json"],
                context=f"recursive model turn {row['id']} metrics",
            ),
            error=(
                None
                if row["error_json"] is None
                else _json_load(
                    row["error_json"],
                    context=f"recursive model turn {row['id']} error",
                )
            ),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _recursive_item_from_row(row: sqlite3.Row) -> RecursiveItemRecord:
        return RecursiveItemRecord(
            id=int(row["id"]),
            session_id=row["session_id"],
            call_id=int(row["call_id"]),
            sequence=int(row["sequence"]),
            model_turn_id=(
                None if row["model_turn_id"] is None else int(row["model_turn_id"])
            ),
            origin=row["origin"],
            payload=_json_load(
                row["payload_json"], context=f"recursive item {row['id']}"
            ),
            created_at=row["created_at"],
        )


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_RECURSION_CONFIG",
    "SESSION_STATUSES",
    "RECURSIVE_CALL_STATUSES",
    "RECURSIVE_MODEL_TURN_STATUSES",
    "RECURSIVE_MODEL_TURN_PURPOSES",
    "SessionStore",
    "SessionRecord",
    "ConversationItemRecord",
    "CheckpointRecord",
    "CompactionRecord",
    "RecursiveCallRecord",
    "RecursiveModelTurnRecord",
    "RecursiveItemRecord",
    "RecursiveCallSnapshot",
    "SessionSnapshot",
    "SessionStoreError",
    "SessionNotFoundError",
    "SchemaVersionError",
    "DatabaseCorruptionError",
    "RecursiveCallNotFoundError",
    "RecursiveLimitError",
]
