import sqlite3
import tempfile
import unittest
from pathlib import Path

from session_store import (
    DatabaseCorruptionError,
    DEFAULT_RECURSION_CONFIG,
    RecursiveLimitError,
    SessionStore,
)


V1_SCHEMA = """
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
    cryosparc_url TEXT,
    cryosparc_email TEXT,
    project_uid TEXT,
    workspace_uid TEXT,
    manifest_json TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE conversation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    sequence INTEGER NOT NULL,
    origin TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);
CREATE INDEX conversation_items_session_sequence
    ON conversation_items(session_id, sequence);
CREATE TRIGGER conversation_items_are_immutable_on_update
BEFORE UPDATE ON conversation_items
BEGIN
    SELECT RAISE(ABORT, 'conversation_items are append-only');
END;
CREATE TRIGGER conversation_items_are_immutable_on_delete
BEFORE DELETE ON conversation_items
BEGIN
    SELECT RAISE(ABORT, 'conversation_items are append-only');
END;
CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    ordinal INTEGER NOT NULL,
    active_item_ids_json TEXT NOT NULL,
    last_summary_item_id INTEGER REFERENCES conversation_items(id),
    durable_state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, ordinal)
);
CREATE INDEX checkpoints_session_ordinal
    ON checkpoints(session_id, ordinal);
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
);
CREATE INDEX compactions_session_ordinal
    ON compactions(session_id, ordinal);
PRAGMA user_version = 1;
"""


class RecursiveSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "sessions.sqlite3"
        self.store = SessionStore(self.database_path)

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def create_session(self, *, recursion_config=None):
        return self.store.create_session(
            provider="openai",
            model="gpt-5.5",
            reasoning={"effort": "high"},
            compact_config={"threshold": 800_000},
            recursion_config=recursion_config,
            manifest={"schema_version": 1},
        )

    def create_root_linkage(self, session_id, suffix="1"):
        return self.store.append_items(
            session_id,
            "model",
            [
                {"role": "user", "content": f"root {suffix}"},
                {
                    "type": "function_call",
                    "name": "run_code",
                    "call_id": f"root-call-{suffix}",
                    "arguments": {"code": "rlm(messages)"},
                },
            ],
        )

    def create_call(self, session_id, *, suffix="1", **overrides):
        root_turn, root_function = self.create_root_linkage(session_id, suffix)
        values = {
            "invoking_call_id": f"root-call-{suffix}",
            "bootstrap_items": [
                {"role": "developer", "content": {"scope": "P1/W1"}},
                {"role": "user", "content": "delegate this"},
            ],
            "root_turn_item_id": root_turn,
            "root_function_call_item_id": root_function,
            "root_function_call_id": f"root-call-{suffix}",
        }
        values.update(overrides)
        return self.store.create_recursive_call(session_id, **values)

    def test_schema_defaults_and_recursive_items_are_isolated_and_append_only(self):
        session_id = self.create_session()
        self.assertEqual(
            self.store.connection.execute("PRAGMA user_version").fetchone()[0], 2
        )
        self.assertEqual(
            self.store.get_session(session_id).recursion_config,
            DEFAULT_RECURSION_CONFIG,
        )
        root_history = self.store.load_conversation(session_id)
        call = self.create_call(session_id)

        unknown_payload = {
            "type": "message",
            "unknown": {"null": None, "false": False},
            "content": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
                    "detail": None,
                }
            ],
        }
        appended = self.store.append_recursive_tool_bundle(call.id, [unknown_payload])

        self.assertEqual(self.store.load_recursive_history(call.id)[-1], unknown_payload)
        self.assertEqual(self.store.load_conversation(session_id)[: len(root_history)], root_history)
        self.assertNotIn(unknown_payload, self.store.load_conversation(session_id))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE recursive_items SET origin = 'changed' WHERE id = ?",
                (appended[0],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM recursive_items WHERE id = ?", (appended[0],)
            )

    def test_nested_lifecycle_records_lineage_turns_tools_and_compaction_order(self):
        session_id = self.create_session()
        call = self.create_call(session_id)
        first_active = list(call.active_item_ids)
        turn = self.store.reserve_recursive_model_turn(call.id)
        model_item = {
            "type": "function_call",
            "call_id": "child-tool-1",
            "name": "run_and_wait",
            "arguments": {"code": "job.status"},
        }
        completed, active = self.store.complete_recursive_model_turn(
            turn.id, [model_item], metrics={"input_tokens": 10}
        )
        tool_ids = self.store.append_recursive_tool_bundle(
            call.id,
            [
                {
                    "type": "function_call_output",
                    "call_id": "child-tool-1",
                    "output": {"status": "completed", "future": None},
                }
            ],
            model_turn_id=turn.id,
        )
        child = self.store.create_recursive_call(
            session_id,
            parent_call_id=call.id,
            invoking_call_id="nested-1",
            bootstrap_items=[{"role": "developer", "content": "same scope"}],
        )

        self.assertEqual(completed.response_item_ids, (active[-1],))
        self.assertEqual(child.depth, 2)
        self.assertEqual(child.ordinal, 1)
        self.assertEqual(child.root_turn_item_id, call.root_turn_item_id)
        self.assertEqual(
            child.root_function_call_item_id, call.root_function_call_item_id
        )
        self.assertEqual(child.root_function_call_id, call.root_function_call_id)
        self.assertEqual(
            self.store.get_recursive_call(call.id).active_item_ids,
            (*first_active, active[-1], *tool_ids),
        )

        branch_active_before_compaction = self.store.get_recursive_call(
            call.id
        ).active_item_ids
        prompt_ids = self.store.append_recursive_trace_items(
            call.id,
            [
                {"role": "developer", "content": "summarize exactly"},
                {"role": "user", "content": {"branch_item_ids": list(branch_active_before_compaction)}},
            ],
            origin="compaction_prompt",
        )
        self.assertEqual(
            self.store.get_recursive_call(call.id).active_item_ids,
            branch_active_before_compaction,
        )
        compaction_turn = self.store.reserve_recursive_model_turn(
            call.id,
            purpose="compaction",
            active_item_ids=[branch_active_before_compaction[0], *prompt_ids],
        )
        raw_summary = {
            "role": "assistant",
            "content": '{"summary":"durable"}',
        }
        raw_turn, preserved_active = self.store.complete_recursive_model_turn(
            compaction_turn.id,
            [raw_summary],
            preserve_call_active_context=True,
        )
        self.assertEqual(preserved_active, list(branch_active_before_compaction))
        self.assertEqual(
            self.store.get_recursive_call(call.id).active_item_ids,
            branch_active_before_compaction,
        )
        retained = [first_active[0], tool_ids[0]]
        summary = {"role": "developer", "content": {"summary": "durable"}}
        summary_ids = self.store.append_recursive_tool_bundle(
            call.id,
            [summary],
            retained_active_item_ids=retained,
            new_item_position=1,
            origin="compaction",
        )
        compacted_active = list(self.store.get_recursive_call(call.id).active_item_ids)
        self.assertEqual(
            compacted_active,
            [retained[0], summary_ids[0], retained[1]],
        )
        self.assertEqual(len(raw_turn.response_item_ids), 1)
        self.assertIn(raw_summary, self.store.load_recursive_history(call.id))
        self.assertEqual(len(self.store.list_recursive_items(call.id)), 8)

        self.store.finalize_recursive_call(child.id, "nested result")
        finished = self.store.finalize_recursive_call(
            call.id, "parent result", metrics={"turns": 2}
        )
        snapshot = self.store.load_recursive_call(call.id, include_descendants=True)
        self.assertEqual(finished.final_output, "parent result")
        self.assertEqual(finished.metrics, {"turns": 2})
        self.assertEqual([record.id for record in snapshot.descendants], [child.id])
        self.assertEqual(len(snapshot.model_turns), 2)
        self.assertIn(summary, snapshot.history)

    def test_limits_are_counted_across_one_root_and_reset_for_another(self):
        session_id = self.create_session(
            recursion_config={"max_depth": 2, "max_calls": 3, "max_model_turns": 2}
        )
        call = self.create_call(session_id)
        child = self.store.create_recursive_call(
            session_id,
            parent_call_id=call.id,
            invoking_call_id="nested-1",
            bootstrap_items=[{"role": "user", "content": "nested"}],
        )
        with self.assertRaises(RecursiveLimitError):
            self.store.create_recursive_call(
                session_id,
                parent_call_id=child.id,
                invoking_call_id="too-deep",
                bootstrap_items=[{"role": "user", "content": "too deep"}],
            )
        sibling = self.store.create_recursive_call(
            session_id,
            parent_call_id=call.id,
            invoking_call_id="nested-2",
            bootstrap_items=[{"role": "user", "content": "sibling"}],
        )
        with self.assertRaises(RecursiveLimitError):
            self.store.create_recursive_call(
                session_id,
                parent_call_id=call.id,
                invoking_call_id="too-many",
                bootstrap_items=[{"role": "user", "content": "too many"}],
            )

        failed_turn = self.store.reserve_recursive_model_turn(call.id)
        self.store.fail_recursive_model_turn(failed_turn.id, {"provider": "failed"})
        completed_turn = self.store.reserve_recursive_model_turn(child.id)
        self.store.complete_recursive_model_turn(completed_turn.id, [])
        with self.assertRaises(RecursiveLimitError):
            self.store.reserve_recursive_model_turn(sibling.id)
        self.assertEqual(
            self.store.get_recursive_call(sibling.id).status, "budget_exhausted"
        )
        self.assertEqual(
            len(
                self.store.connection.execute(
                    "SELECT id FROM recursive_model_turns WHERE root_turn_item_id = ?",
                    (call.root_turn_item_id,),
                ).fetchall()
            ),
            2,
        )

        other_root = self.create_call(session_id, suffix="2")
        self.assertEqual(
            self.store.reserve_recursive_model_turn(other_root.id).aggregate_ordinal, 0
        )

    def test_tool_bundle_and_root_checkpoint_roll_back_as_one_transaction(self):
        session_id = self.create_session()
        call = self.create_call(session_id)
        root_ids = self.store.append_items(
            session_id, "input", [{"role": "user", "content": "checkpoint"}]
        )
        before_history = self.store.load_recursive_history(call.id)
        before_active = self.store.get_recursive_call(call.id).active_item_ids
        self.store.connection.execute(
            """
            CREATE TRIGGER reject_recursive_checkpoint
            BEFORE INSERT ON checkpoints
            BEGIN
                SELECT RAISE(ABORT, 'forced recursive checkpoint failure');
            END
            """
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "forced recursive checkpoint failure"
        ):
            self.store.append_recursive_tool_bundle_and_checkpoint(
                call.id,
                [{"type": "function_call_output", "output": "rollback"}],
                root_active_item_ids=root_ids,
                durable_state={"jobs": ["J1"]},
            )

        self.assertEqual(self.store.load_recursive_history(call.id), before_history)
        self.assertEqual(
            self.store.get_recursive_call(call.id).active_item_ids, before_active
        )
        self.assertEqual(self.store.list_checkpoints(session_id), [])

    def test_recovery_interrupts_deepest_first_and_never_replays(self):
        session_id = self.create_session()
        call = self.create_call(session_id)
        reserved = self.store.reserve_recursive_model_turn(call.id)
        child = self.store.create_recursive_call(
            session_id,
            parent_call_id=call.id,
            invoking_call_id="nested",
            bootstrap_items=[{"role": "user", "content": "child"}],
        )
        recovered = self.store.recover_incomplete_recursive_calls(session_id)

        self.assertEqual([record.id for record in recovered], [child.id, call.id])
        self.assertEqual(
            self.store.get_recursive_model_turn(reserved.id).status, "interrupted"
        )
        self.assertEqual(
            [record.status for record in self.store.list_recursive_calls(session_id)],
            ["interrupted", "interrupted"],
        )
        self.assertEqual(
            [item.origin for item in self.store.list_recursive_items(call.id)],
            ["bootstrap", "bootstrap", "recovery"],
        )
        self.assertEqual(self.store.recover_incomplete_recursive_calls(session_id), [])

    def test_cross_session_lineage_and_direct_foreign_keys_are_rejected(self):
        first_session = self.create_session()
        second_session = self.create_session()
        first_call = self.create_call(first_session)
        second_root, second_function = self.create_root_linkage(second_session, "other")

        with self.assertRaises(ValueError):
            self.store.create_recursive_call(
                second_session,
                parent_call_id=first_call.id,
                invoking_call_id="cross-session",
                bootstrap_items=[],
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                """
                INSERT INTO recursive_calls (
                    session_id, root_turn_item_id, root_function_call_item_id,
                    root_function_call_id, parent_call_id, invoking_call_id,
                    depth, ordinal, status, active_item_ids_json,
                    metrics_json, started_at, updated_at
                ) VALUES (?, ?, ?, 'bad', NULL, 'bad', 1, 99, 'running',
                          '[]', '{}', 'now', 'now')
                """,
                (
                    first_session,
                    second_root,
                    second_function,
                ),
            )

        other_root, other_function = self.create_root_linkage(first_session, "same")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                """
                INSERT INTO recursive_calls (
                    session_id, root_turn_item_id, root_function_call_item_id,
                    root_function_call_id, parent_call_id, invoking_call_id,
                    depth, ordinal, status, active_item_ids_json,
                    metrics_json, started_at, updated_at
                ) VALUES (?, ?, ?, 'other-root', ?, 'bad-parent', 2, 99,
                          'running', '[]', '{}', 'now', 'now')
                """,
                (
                    first_session,
                    other_root,
                    other_function,
                    first_call.id,
                ),
            )
        self.assertEqual(
            self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), []
        )

    def test_v1_migration_backfills_defaults_and_preserves_root_state(self):
        self.store.close()
        self.database_path.unlink()
        connection = sqlite3.connect(self.database_path)
        connection.executescript(V1_SCHEMA)
        connection.execute(
            """
            INSERT INTO sessions (
                id, created_at, updated_at, status, provider, model,
                reasoning_json, compact_config_json, manifest_json
            ) VALUES ('legacy', 'then', 'then', 'running', 'openai', 'gpt',
                      '{"effort":"high"}', '{"threshold":1}', '{"v":1}')
            """
        )
        connection.execute(
            """
            INSERT INTO conversation_items (
                session_id, sequence, origin, payload_json, created_at
            ) VALUES ('legacy', 0, 'input', '{"role":"user","content":"keep"}', 'then')
            """
        )
        connection.commit()
        connection.close()

        self.store = SessionStore(self.database_path)

        self.assertEqual(
            self.store.connection.execute("PRAGMA user_version").fetchone()[0], 2
        )
        self.assertEqual(
            self.store.get_session("legacy").recursion_config,
            DEFAULT_RECURSION_CONFIG,
        )
        self.assertEqual(
            self.store.load_conversation("legacy"),
            [{"role": "user", "content": "keep"}],
        )
        self.store.close()
        self.store = SessionStore(self.database_path)
        self.assertEqual(self.store.list_recursive_calls("legacy"), [])

    def test_late_migration_failure_rolls_back_every_v2_change(self):
        self.store.close()
        self.database_path.unlink()
        connection = sqlite3.connect(self.database_path)
        connection.executescript(V1_SCHEMA)
        connection.commit()
        connection.close()

        class FailingMigrationStore(SessionStore):
            @staticmethod
            def _recursive_schema_statements():
                return (
                    *SessionStore._recursive_schema_statements(),
                    "CREATE TABLE deliberately_invalid (",
                )

        with self.assertRaises(DatabaseCorruptionError):
            FailingMigrationStore(self.database_path)

        connection = sqlite3.connect(self.database_path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        connection.close()
        self.assertNotIn("recursion_config_json", session_columns)
        self.assertNotIn("recursive_calls", tables)
        self.store = SessionStore(self.database_path)


if __name__ == "__main__":
    unittest.main()
