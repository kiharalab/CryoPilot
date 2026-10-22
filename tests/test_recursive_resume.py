import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import harness
from session_store import SessionStore
from startup_cli import bind_manifest_scope


def manifest_template():
    return {
        "schema_version": 1,
        "session": {"id": None},
        "project": {
            "title": "Recursive resume test",
            "parent_directory": "/cryosparc/projects",
            "project_uid": None,
            "workspace_title": "Automated Reconstruction",
            "workspace_uid": None,
        },
        "dataset": {
            "kind": "micrographs",
            "path": "/data/*.mrc",
            "particle_metadata_path": None,
            "gain_reference_path": None,
            "defect_file_path": None,
            "eer": {"enabled": False, "fractions": None, "upsampling": None},
        },
        "acquisition": {
            "pixel_size_A": 1.0,
            "accelerating_voltage_kV": 300.0,
            "spherical_aberration_mm": 2.7,
            "total_dose_e_per_A2": 50.0,
            "negative_stain": False,
            "phase_plate": False,
        },
        "sample": {
            "description": "test specimen",
            "particle_diameter_A": 150.0,
            "molecular_mass_kDa": None,
            "expected_symmetry": "C1",
        },
        "user_goal": "Test durable recursive resume behavior",
    }


class FakeController:
    def __init__(self, uid, description):
        self.uid = uid
        self.model = SimpleNamespace(uid=uid, description=description)


class FakeProject(FakeController):
    def __init__(self, uid, description):
        super().__init__(uid, description)
        self.workspaces = {}

    def find_workspaces(self):
        return list(self.workspaces.values())


class FakeCryoSPARCConnection:
    def __init__(self, workflow):
        self.workflow = workflow

    def find_projects(self):
        return list(self.workflow.projects.values())


class FakeWorkflow:
    def __init__(self):
        self.projects = {}
        self.cs = FakeCryoSPARCConnection(self)
        self.project = None
        self.workspace = None
        self.project_creations = []
        self.workspace_creations = []

    def create_project(self, title, project_dir=None, desc=None):
        uid = f"P{len(self.project_creations) + 1}"
        project = FakeProject(uid, desc)
        self.projects[uid] = project
        self.project_creations.append(
            {"title": title, "project_dir": project_dir, "desc": desc}
        )
        return project

    def use_project(self, uid):
        self.project = self.projects[uid]
        self.workspace = None
        return self.project

    def create_workspace(self, title, desc=None):
        uid = f"W{len(self.workspace_creations) + 1}"
        workspace = FakeController(uid, desc)
        self.project.workspaces[uid] = workspace
        self.workspace_creations.append({"title": title, "desc": desc})
        return workspace

    def use_workspace(self, uid):
        self.workspace = self.project.workspaces[uid]
        return self.workspace

    def list_jobs(self, limit=20):
        return []


class RecursiveResumeTests(unittest.TestCase):
    _HARNESS_GLOBALS = (
        "client",
        "MODEL",
        "REASONING_EFFORT",
        "use_reasoning_effort",
        "use_native_openai",
        "CONTEXT_COMPACTION_ENABLED",
        "CONTEXT_WINDOW",
        "CONTEXT_COMPACTION_THRESHOLD",
        "CONTEXT_COMPACTION_TARGET_RATIO",
        "COMPACT_MODEL",
        "COMPACT_MAX_TOKENS",
        "COMPACT_REASONING_EFFORT",
        "TRACE_TOOLS",
    )
    _PYTHON_GLOBAL_KEYS = (
        "csw",
        "session_context",
        "session_state",
        "conversation_history",
    )

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "recursive-resume.sqlite3"
        )
        self.store = SessionStore(self.database_path)
        self.saved_globals = {
            name: getattr(harness, name) for name in self._HARNESS_GLOBALS
        }
        self.saved_memory = copy.deepcopy(harness.agent_memory)
        self.saved_runtime = {
            key: value if key == "store" else copy.deepcopy(value)
            for key, value in harness.session_runtime.items()
        }
        self.missing = object()
        self.saved_python_globals = {
            key: harness.python_globals.get(key, self.missing)
            for key in self._PYTHON_GLOBAL_KEYS
        }
        harness.TRACE_TOOLS = False

    def tearDown(self):
        for name, value in self.saved_globals.items():
            setattr(harness, name, value)
        harness.agent_memory.clear()
        harness.agent_memory.update(copy.deepcopy(self.saved_memory))
        harness.session_runtime.clear()
        harness.session_runtime.update(self.saved_runtime)
        for key, value in self.saved_python_globals.items():
            if value is self.missing:
                harness.python_globals.pop(key, None)
            else:
                harness.python_globals[key] = value
        self.store.close()
        self.temporary_directory.cleanup()

    @staticmethod
    def credentials():
        return {
            "url": "https://cryosparc.example.test",
            "email": "scientist@example.test",
            "password": "never-persist-this-password",
        }

    def create_session(
        self,
        *,
        recursion_config=None,
        status="running",
        awaiting_continuation=False,
    ):
        session_id = self.store.create_session(
            provider="openai",
            model="gpt-5.5",
            reasoning={"effort": "high"},
            compact_config={"enabled": False},
            recursion_config=recursion_config,
            cryosparc_url="https://cryosparc.example.test",
            cryosparc_email="scientist@example.test",
            manifest=manifest_template(),
            status=status,
        )
        self.store.update_scope(session_id, project_uid="P9")
        self.store.update_scope(session_id, workspace_uid="W4")
        bound = bind_manifest_scope(
            manifest_template(),
            session_id=session_id,
            project_uid="P9",
            workspace_uid="W4",
        )
        self.store.update_manifest(session_id, bound)
        root_ids = self.store.append_items(
            session_id,
            "bootstrap",
            [
                {"role": "developer", "content": "trusted scope"},
                {"role": "user", "content": "root request"},
                {"role": "assistant", "content": "work in progress"},
            ],
        )
        self.store.save_checkpoint(
            session_id,
            root_ids,
            durable_state={
                "known_jobs": [],
                "awaiting_continuation": awaiting_continuation,
            },
        )
        return session_id, root_ids

    def create_running_recursive_call(self, session_id, root_turn_item_id):
        root_function_item_id = self.store.append_items(
            session_id,
            "model",
            [
                {
                    "type": "function_call",
                    "name": "run_code",
                    "call_id": "root-run-call",
                    "arguments": '{"code":"rlm(messages)"}',
                }
            ],
        )[0]
        call = self.store.create_recursive_call(
            session_id,
            invoking_call_id="root-run-call",
            bootstrap_items=[
                {"role": "developer", "content": "trusted child scope"},
                {"role": "user", "content": "inspect a bounded subproblem"},
            ],
            root_turn_item_id=root_turn_item_id,
            root_function_call_item_id=root_function_item_id,
            root_function_call_id="root-run-call",
        )
        turn = self.store.reserve_recursive_model_turn(call.id)
        return call, turn

    def bound_workflow(self, session_id):
        marker = harness.session_scope_marker(session_id)
        workflow = FakeWorkflow()
        project = FakeProject("P9", marker)
        project.workspaces["W4"] = FakeController("W4", marker)
        workflow.projects["P9"] = project
        return workflow

    def test_new_session_persists_explicit_recursion_limits(self):
        workflow = FakeWorkflow()
        captured = {}

        def fake_run_agent(prompt=None, developer_instructions=None, resume=False):
            captured["runtime_config"] = copy.deepcopy(
                harness.session_runtime["recursion_config"]
            )

        with (
            mock.patch.object(harness, "configure_model_client"),
            mock.patch.object(
                harness,
                "collect_cryosparc_credentials",
                return_value=self.credentials(),
            ),
            mock.patch.object(harness, "connect_cryosparc", return_value=workflow),
            mock.patch.object(
                harness, "collect_session_manifest", return_value=manifest_template()
            ),
            mock.patch.object(harness, "run_agent", side_effect=fake_run_agent),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = harness.main(
                [
                    "--session-db",
                    str(self.database_path),
                    "--rlm-max-depth",
                    "6",
                    "--rlm-max-calls",
                    "9",
                    "--rlm-max-model-turns",
                    "12",
                ]
            )

        expected = {"max_depth": 6, "max_calls": 9, "max_model_turns": 12}
        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["runtime_config"], expected)
        session_id = self.store.connection.execute(
            "SELECT id FROM sessions"
        ).fetchone()[0]
        self.assertEqual(self.store.get_session(session_id).recursion_config, expected)

    def test_resume_restores_saved_recursion_limits(self):
        expected = {"max_depth": 2, "max_calls": 5, "max_model_turns": 7}
        session_id, _root_ids = self.create_session(recursion_config=expected)
        workflow = self.bound_workflow(session_id)
        captured = {}

        def fake_run_agent(prompt=None, developer_instructions=None, resume=False):
            captured["resume"] = resume
            captured["runtime_config"] = copy.deepcopy(
                harness.session_runtime["recursion_config"]
            )

        with (
            mock.patch.object(harness, "configure_model_client"),
            mock.patch.object(
                harness,
                "collect_cryosparc_credentials",
                return_value=self.credentials(),
            ),
            mock.patch.object(harness, "connect_cryosparc", return_value=workflow),
            mock.patch.object(harness, "run_agent", side_effect=fake_run_agent),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = harness.main(
                ["--session-db", str(self.database_path), "--resume", session_id]
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(captured["resume"])
        self.assertEqual(captured["runtime_config"], expected)
        self.assertEqual(self.store.get_session(session_id).recursion_config, expected)

    def test_resume_rejects_each_recursion_limit_override_before_login(self):
        session_id, _root_ids = self.create_session()

        for arguments in (
            ["--rlm-max-depth", "3"],
            ["--rlm-max-calls", "8"],
            ["--rlm-max-model-turns", "13"],
        ):
            with self.subTest(arguments=arguments):
                with (
                    mock.patch.object(
                        harness, "collect_cryosparc_credentials"
                    ) as collect_credentials,
                    mock.patch.object(harness, "connect_cryosparc") as connect,
                ):
                    with self.assertRaisesRegex(
                        ValueError, "recursion limits"
                    ):
                        harness.main(
                            [
                                "--session-db",
                                str(self.database_path),
                                "--resume",
                                session_id,
                                *arguments,
                            ]
                        )
                collect_credentials.assert_not_called()
                connect.assert_not_called()

    def test_failed_reconnect_or_scope_bind_leaves_recursive_call_running(self):
        session_id, root_ids = self.create_session()
        call, turn = self.create_running_recursive_call(session_id, root_ids[1])
        workflow = self.bound_workflow(session_id)

        failures = (
            {
                "name": "connect",
                "connect": mock.DEFAULT,
                "ensure": None,
            },
            {
                "name": "scope",
                "connect": workflow,
                "ensure": RuntimeError("scope bind failed"),
            },
        )
        for failure in failures:
            with self.subTest(boundary=failure["name"]):
                connect_patch = (
                    mock.patch.object(
                        harness,
                        "connect_cryosparc",
                        side_effect=RuntimeError("connect failed"),
                    )
                    if failure["connect"] is mock.DEFAULT
                    else mock.patch.object(
                        harness, "connect_cryosparc", return_value=failure["connect"]
                    )
                )
                ensure_patch = (
                    mock.patch.object(
                        harness,
                        "ensure_session_scope",
                        side_effect=failure["ensure"],
                    )
                    if failure["ensure"] is not None
                    else contextlib.nullcontext()
                )
                with (
                    mock.patch.object(harness, "configure_model_client"),
                    mock.patch.object(
                        harness,
                        "collect_cryosparc_credentials",
                        return_value=self.credentials(),
                    ),
                    connect_patch,
                    ensure_patch,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, f"{failure['name']}.*failed"
                    ):
                        harness.main(
                            [
                                "--session-db",
                                str(self.database_path),
                                "--resume",
                                session_id,
                            ]
                        )

                self.assertEqual(
                    self.store.get_recursive_call(call.id).status, "running"
                )
                self.assertEqual(
                    self.store.get_recursive_model_turn(turn.id).status, "reserved"
                )
                self.assertEqual(
                    [item.origin for item in self.store.list_recursive_items(call.id)],
                    ["bootstrap", "bootstrap"],
                )

    def test_successful_scope_bind_interrupts_without_replay_and_notes_call_ids(self):
        session_id, root_ids = self.create_session()
        call, turn = self.create_running_recursive_call(session_id, root_ids[1])
        workflow = self.bound_workflow(session_id)
        before_history = self.store.load_recursive_history(call.id)
        events = []
        captured = {}
        real_ensure = harness.ensure_session_scope
        real_recover = harness.recover_interrupted_recursive_calls

        def tracked_ensure(csw, store, active_session_id):
            events.append("scope-bound")
            return real_ensure(csw, store, active_session_id)

        def tracked_recover(store, active_session_id):
            self.assertEqual(events, ["scope-bound"])
            self.assertIs(harness.python_globals["csw"], workflow)
            events.append("recursive-recovery")
            return real_recover(store, active_session_id)

        def fake_run_agent(prompt=None, developer_instructions=None, resume=False):
            events.append("root-resume")
            captured["resume"] = resume
            captured["active"] = copy.deepcopy(
                harness.agent_memory["active_messages"]
            )

        with (
            mock.patch.object(harness, "configure_model_client"),
            mock.patch.object(
                harness,
                "collect_cryosparc_credentials",
                return_value=self.credentials(),
            ),
            mock.patch.object(harness, "connect_cryosparc", return_value=workflow),
            mock.patch.object(
                harness, "ensure_session_scope", side_effect=tracked_ensure
            ),
            mock.patch.object(
                harness,
                "recover_interrupted_recursive_calls",
                side_effect=tracked_recover,
            ),
            mock.patch.object(harness, "run_recursive_child") as replay_child,
            mock.patch.object(harness, "run_agent", side_effect=fake_run_agent),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = harness.main(
                ["--session-db", str(self.database_path), "--resume", session_id]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            events, ["scope-bound", "recursive-recovery", "root-resume"]
        )
        replay_child.assert_not_called()
        self.assertTrue(captured["resume"])
        recovered = self.store.get_recursive_call(call.id)
        self.assertEqual(recovered.status, "interrupted")
        self.assertEqual(
            self.store.get_recursive_model_turn(turn.id).status, "interrupted"
        )
        after_history = self.store.load_recursive_history(call.id)
        self.assertEqual(after_history[:-1], before_history)
        self.assertEqual(after_history[-1]["type"], "recursive_recovery")

        resume_notes = [
            item["content"]
            for item in captured["active"]
            if item.get("role") == "developer"
            and str(item.get("content", "")).startswith("[SESSION RESUME]")
        ]
        self.assertEqual(len(resume_notes), 1)
        self.assertIn(str(call.id), resume_notes[0])
        self.assertIn("were not replayed", resume_notes[0])
        self.assertIn("rlm.history(call_id)", resume_notes[0])
        self.assertNotIn(self.credentials()["password"], json.dumps(after_history))


if __name__ == "__main__":
    unittest.main()
