import sqlite3
import tempfile
import unittest
from pathlib import Path

from session_store import (
    DatabaseCorruptionError,
    SchemaVersionError,
    SessionNotFoundError,
    SessionStore,
)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "sessions.sqlite3"
        self.store = SessionStore(self.database_path)

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def create_session(self, **overrides):
        values = {
            "provider": "openai",
            "model": "gpt-5.5",
            "reasoning": {"effort": "high"},
            "compact_config": {"threshold": 800_000},
            "cryosparc_url": "https://cryosparc.example.test",
            "cryosparc_email": "scientist@example.test",
            "manifest": {"schema_version": 1, "project": {"project_uid": None}},
        }
        values.update(overrides)
        return self.store.create_session(**values)

    def test_schema_pragmas_and_uuid_session_metadata(self):
        self.assertEqual(
            self.store.connection.execute("PRAGMA user_version").fetchone()[0], 2
        )
        self.assertEqual(
            self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1
        )
        self.assertEqual(
            self.store.connection.execute("PRAGMA journal_mode").fetchone()[0],
            "wal",
        )
        self.assertEqual(
            self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0],
            5_000,
        )

        session_id = self.create_session()
        self.assertEqual(len(session_id), 32)
        int(session_id, 16)
        session = self.store.get_session(session_id)
        self.assertEqual(session.status, "collecting")
        self.assertEqual(session.reasoning, {"effort": "high"})
        self.assertEqual(session.compact_config, {"threshold": 800_000})
        self.assertIsNone(session.project_uid)
        self.assertIsNone(session.last_error)

    def test_exact_json_round_trip_preserves_unknown_fields_and_nulls(self):
        session_id = self.create_session()
        items = [
            {
                "type": "message",
                "role": "assistant",
                "phase": "analysis",
                "unknown_future_field": {"nested": None, "flag": False},
                "content": [
                    {
                        "type": "output_text",
                        "text": "inspect the map",
                        "annotations": [],
                    }
                ],
            },
            {
                "type": "reasoning",
                "id": "rs_123",
                "encrypted_content": "opaque-encrypted-reasoning-value",
                "summary": [],
            },
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": [
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
                        "detail": None,
                    }
                ],
            },
        ]

        ids = self.store.append_items(session_id, "model", items)

        self.assertEqual(len(ids), 3)
        self.assertEqual(self.store.load_conversation(session_id), items)
        records = self.store.list_conversation_items(session_id)
        self.assertEqual([record.sequence for record in records], [0, 1, 2])
        self.assertEqual([record.origin for record in records], ["model"] * 3)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE conversation_items SET origin = 'changed' WHERE id = ?",
                (ids[0],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM conversation_items WHERE id = ?", (ids[0],)
            )

    def test_active_order_summary_and_durable_state_restore(self):
        session_id = self.create_session()
        developer, summary, tail = self.store.append_items(
            session_id,
            "bootstrap",
            [
                {"role": "developer", "content": "scope"},
                {"role": "developer", "content": {"current_stage": "2D"}},
                {"role": "user", "content": "continue"},
            ],
        )
        durable_state = {
            "jobs": [{"uid": "J42", "status": "running", "result": None}],
            "future": {"unknown": 7},
        }
        checkpoint_id = self.store.save_checkpoint(
            session_id,
            [developer, summary, tail],
            last_summary_item_id=summary,
            durable_state=durable_state,
        )

        snapshot = self.store.load_snapshot(session_id)

        self.assertEqual(snapshot.checkpoint.id, checkpoint_id)
        self.assertEqual(
            snapshot.checkpoint.active_item_ids, (developer, summary, tail)
        )
        self.assertEqual(snapshot.checkpoint.durable_state, durable_state)
        self.assertEqual(
            snapshot.active_messages,
            [
                {"role": "developer", "content": "scope"},
                {"role": "developer", "content": {"current_stage": "2D"}},
                {"role": "user", "content": "continue"},
            ],
        )
        self.assertEqual(
            snapshot.last_summary,
            {"role": "developer", "content": {"current_stage": "2D"}},
        )

    def test_compaction_history_retains_before_after_and_metrics(self):
        session_id = self.create_session()
        initial_ids = self.store.append_items(
            session_id,
            "input",
            [
                {"role": "developer", "content": "scope"},
                {"role": "user", "content": "reconstruct"},
                {"role": "assistant", "content": "working"},
            ],
        )
        summary_item_id, checkpoint_id, compaction_id, after_ids = (
            self.store.append_compaction_and_checkpoint(
                session_id,
                {
                    "role": "developer",
                    "content": {
                        "schema": "cryopilot_compaction_v1",
                        "current_stage": "motion correction",
                    },
                },
                reason="context_threshold",
                before_active_item_ids=initial_ids,
                retained_active_item_ids=[initial_ids[0], initial_ids[2]],
                summary_position=1,
                metrics={"before_tokens": 900_000, "after_tokens": 10_000},
                durable_state={"jobs": ["J1"]},
            )
        )

        records = self.store.list_compactions(session_id)

        self.assertEqual(after_ids, [initial_ids[0], summary_item_id, initial_ids[2]])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].id, compaction_id)
        self.assertEqual(records[0].ordinal, 0)
        self.assertEqual(records[0].before_active_item_ids, tuple(initial_ids))
        self.assertEqual(records[0].after_active_item_ids, tuple(after_ids))
        self.assertEqual(
            records[0].metrics,
            {"before_tokens": 900_000, "after_tokens": 10_000},
        )
        self.assertEqual(
            self.store.load_snapshot(session_id).checkpoint.active_item_ids,
            tuple(after_ids),
        )
        self.assertEqual(self.store.load_snapshot(session_id).checkpoint.id, checkpoint_id)

    def test_compaction_append_checkpoint_and_record_roll_back_together(self):
        session_id = self.create_session()
        initial_items = [
            {"role": "developer", "content": "scope"},
            {"role": "user", "content": "reconstruct"},
            {"role": "assistant", "content": "working"},
        ]
        initial_ids = self.store.append_items(session_id, "input", initial_items)
        initial_checkpoint = self.store.save_checkpoint(session_id, initial_ids)
        self.store.connection.execute(
            """
            CREATE TRIGGER reject_compaction_record
            BEFORE INSERT ON compactions
            BEGIN
                SELECT RAISE(ABORT, 'forced compaction metadata failure');
            END
            """
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced compaction metadata failure"):
            self.store.append_compaction_and_checkpoint(
                session_id,
                {"role": "developer", "content": "must roll back"},
                reason="context_threshold",
                before_active_item_ids=initial_ids,
                retained_active_item_ids=[initial_ids[0], initial_ids[2]],
                summary_position=1,
                metrics={"before_tokens": 900_000},
                durable_state={"jobs": ["J1"]},
            )

        self.assertEqual(self.store.load_conversation(session_id), initial_items)
        checkpoints = self.store.list_checkpoints(session_id)
        self.assertEqual([record.id for record in checkpoints], [initial_checkpoint])
        self.assertEqual(self.store.list_compactions(session_id), [])
        self.assertEqual(
            self.store.load_snapshot(session_id).checkpoint.active_item_ids,
            tuple(initial_ids),
        )

    def test_append_and_checkpoint_is_atomic_on_invalid_context(self):
        session_id = self.create_session()
        with self.assertRaises(ValueError):
            self.store.append_items_and_checkpoint(
                session_id,
                "model",
                [{"type": "message", "content": "must roll back"}],
                active_item_ids=[987_654_321],
            )

        self.assertEqual(self.store.load_conversation(session_id), [])
        self.assertEqual(self.store.list_checkpoints(session_id), [])

        ids, checkpoint_id = self.store.append_items_and_checkpoint(
            session_id,
            "model",
            [{"type": "message", "content": "committed"}],
            durable_state={"safe": True},
        )
        self.assertEqual(len(ids), 1)
        self.assertGreater(checkpoint_id, 0)
        snapshot = self.store.load_snapshot(session_id)
        self.assertEqual(snapshot.checkpoint.active_item_ids, tuple(ids))
        self.assertEqual(snapshot.active_messages[0]["content"], "committed")

    def test_updates_scope_manifest_and_status_without_credentials(self):
        session_id = self.create_session()
        self.store.update_scope(session_id, project_uid="P12")
        self.store.update_scope(session_id, workspace_uid="W3")
        manifest = {
            "schema_version": 1,
            "project": {"project_uid": "P12", "workspace_uid": "W3"},
        }
        self.store.update_manifest(session_id, manifest)
        self.store.set_status(session_id, "interrupted", last_error="Ctrl+C")

        session = self.store.get_session(session_id)
        self.assertEqual(session.project_uid, "P12")
        self.assertEqual(session.workspace_uid, "W3")
        self.assertEqual(session.manifest, manifest)
        self.assertEqual(session.status, "interrupted")
        self.assertEqual(session.last_error, "Ctrl+C")

        columns = {
            row[1]
            for row in self.store.connection.execute("PRAGMA table_info(sessions)")
        }
        forbidden = {"password", "api_key", "access_token", "refresh_token", "secret"}
        self.assertTrue(columns.isdisjoint(forbidden))
        with self.assertRaises(TypeError):
            self.store.create_session(
                provider="openai",
                model="gpt-5.5",
                password="must-not-be-accepted",
            )

    def test_missing_session_and_invalid_status_are_clear_errors(self):
        with self.assertRaisesRegex(SessionNotFoundError, "missing-session"):
            self.store.get_session("missing-session")
        with self.assertRaises(ValueError):
            self.create_session(status="paused")

    def test_corrupt_database_is_rejected(self):
        self.store.close()
        self.database_path.write_bytes(b"this is not a sqlite database")

        with self.assertRaises(DatabaseCorruptionError):
            SessionStore(self.database_path)

        # Prevent tearDown from trying to use the closed store.
        self.store = SessionStore(Path(self.temporary_directory.name) / "replacement.sqlite3")

    def test_newer_or_unversioned_existing_schema_is_rejected(self):
        self.store.close()
        newer_path = Path(self.temporary_directory.name) / "newer.sqlite3"
        connection = sqlite3.connect(newer_path)
        connection.execute("PRAGMA user_version = 3")
        connection.close()

        with self.assertRaisesRegex(SchemaVersionError, "newer"):
            SessionStore(newer_path)

        unversioned_path = Path(self.temporary_directory.name) / "unversioned.sqlite3"
        connection = sqlite3.connect(unversioned_path)
        connection.execute("CREATE TABLE mystery (value TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(SchemaVersionError, "no recognized schema"):
            SessionStore(unversioned_path)

        self.store = SessionStore(Path(self.temporary_directory.name) / "replacement.sqlite3")

    def test_invalid_stored_json_is_reported_as_corruption(self):
        session_id = self.create_session()
        # Session metadata is mutable by design; use it to simulate disk/manual
        # corruption without weakening append-only transcript guarantees.
        self.store.connection.execute(
            "UPDATE sessions SET manifest_json = ? WHERE id = ?",
            ("{not json", session_id),
        )
        with self.assertRaises(DatabaseCorruptionError):
            self.store.get_session(session_id)


if __name__ == "__main__":
    unittest.main()
