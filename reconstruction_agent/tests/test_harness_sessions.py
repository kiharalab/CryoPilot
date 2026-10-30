import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import harness
import llm_client
from session_store import SessionStore
from startup_cli import bind_manifest_scope


def manifest_template():
    return {
        "schema_version": 1,
        "session": {"id": None},
        "project": {
            "title": "Session reconstruction",
            "parent_directory": "/cryosparc/projects",
            "project_uid": None,
            "workspace_title": "Automated Reconstruction",
            "workspace_uid": None,
        },
        "dataset": {
            "kind": "movies",
            "path": "/data/*.eer",
            "gain_reference_path": None,
            "defect_file_path": None,
            "eer": {"enabled": True, "fractions": 40, "upsampling": 2},
        },
        "acquisition": {
            "pixel_size_A": 0.8,
            "accelerating_voltage_kV": 300.0,
            "spherical_aberration_mm": 2.7,
            "total_dose_e_per_A2": 50.0,
            "negative_stain": False,
            "phase_plate": False,
        },
        "sample": {
            "description": "membrane complex",
            "particle_diameter_A": 180.0,
            "molecular_mass_kDa": None,
            "expected_symmetry": "C1",
        },
        "user_goal": "Find a rare conformational state",
    }


def structured_summary():
    summary = harness.empty_compaction_summary()
    summary.update(
        {
            "current_goal": "Find a rare conformational state.",
            "current_stage": "2D classification",
            "observations": ["J12 contains a small high-quality class."],
            "next_actions": ["Inspect J12 before creating a reconstruction."],
            "memory_search_terms": ["J12", "rare class"],
        }
    )
    return summary


class FakeResponses:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("Unexpected extra model request")
        return self._responses.pop(0)


class FakeClient(llm_client.OpenAIResponsesClient):
    def __init__(self, *responses, native=True, reasoning_effort=None):
        super().__init__(
            SimpleNamespace(responses=FakeResponses(responses)),
            native=native,
            reasoning_effort=reasoning_effort,
        )
        self.responses = self.sdk.responses


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
        if uid not in self.projects:
            raise KeyError(uid)
        self.project = self.projects[uid]
        self.workspace = None
        return self.project

    def create_workspace(self, title, desc=None):
        if self.project is None:
            raise AssertionError("A project must be selected first")
        uid = f"W{len(self.workspace_creations) + 1}"
        workspace = FakeController(uid, desc)
        self.project.workspaces[uid] = workspace
        self.workspace_creations.append({"title": title, "desc": desc})
        return workspace

    def use_workspace(self, uid):
        if self.project is None or uid not in self.project.workspaces:
            raise KeyError(uid)
        self.workspace = self.project.workspaces[uid]
        return self.workspace

    def list_jobs(self, limit=20):
        return []


class HarnessSessionTests(unittest.TestCase):
    _HARNESS_GLOBALS = (
        "client",
        "MODEL",
        "REASONING_EFFORT",
        "CONTEXT_COMPACTION_ENABLED",
        "TRACE_TOOLS",
    )
    _PYTHON_GLOBAL_KEYS = ("csw", "session_context", "session_state")

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temp_directory.name) / "sessions.sqlite3")
        self.saved_globals = {
            name: getattr(harness, name) for name in self._HARNESS_GLOBALS
        }
        self.saved_memory = copy.deepcopy(harness.agent_memory)
        self.saved_runtime = {
            "store": harness.session_runtime["store"],
            "session_id": harness.session_runtime["session_id"],
            "active_item_ids": list(harness.session_runtime["active_item_ids"]),
            "last_summary_item_id": harness.session_runtime["last_summary_item_id"],
            "durable_state": copy.deepcopy(harness.session_runtime["durable_state"]),
            "sensitive_values": list(harness.session_runtime.get("sensitive_values", [])),
        }
        self.missing = object()
        self.saved_python_globals = {
            key: harness.python_globals.get(key, self.missing)
            for key in self._PYTHON_GLOBAL_KEYS
        }

        harness.TRACE_TOOLS = False
        harness.CONTEXT_COMPACTION_ENABLED = False

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
        self.temp_directory.cleanup()

    def create_session(self, *, status="initializing"):
        return self.store.create_session(
            provider="openai",
            model="gpt-5.5",
            reasoning={"effort": "xhigh"},
            compact_config={"enabled": True},
            cryosparc_url="https://cryosparc.example.test",
            cryosparc_email="scientist@example.test",
            manifest=manifest_template(),
            status=status,
        )

    def activate(self, session_id):
        harness.configure_session_runtime(self.store, session_id)

    def test_run_agent_persists_bootstrap_exact_model_items_and_checkpoints(self):
        session_id = self.create_session(status="initializing")
        self.activate(session_id)
        exact_output = {
            "type": "message",
            "id": "msg_exact",
            "role": "assistant",
            "phase": "final",
            "unknown_future_field": {"nullable": None},
            "content": [
                {
                    "type": "output_text",
                    "text": "Reconstruction session initialized.",
                    "annotations": [],
                }
            ],
        }
        response = SimpleNamespace(
            output=[copy.deepcopy(exact_output)],
            output_text="Reconstruction session initialized.",
        )
        harness.client = FakeClient(response)

        with contextlib.redirect_stdout(io.StringIO()):
            harness.run_agent(
                "bootstrap user prompt",
                developer_instructions="project-scoped developer prompt",
            )

        expected = [
            {"role": "developer", "content": "project-scoped developer prompt"},
            {"role": "user", "content": "bootstrap user prompt"},
            exact_output,
        ]
        self.assertEqual(self.store.load_conversation(session_id), expected)
        self.assertEqual(harness.agent_memory["full_history"], expected)
        self.assertEqual(harness.agent_memory["active_messages"], expected)
        self.assertEqual(
            [record.origin for record in self.store.list_conversation_items(session_id)],
            ["bootstrap", "bootstrap", "model"],
        )
        checkpoints = self.store.list_checkpoints(session_id)
        self.assertGreaterEqual(len(checkpoints), 3)
        self.assertEqual(
            list(checkpoints[-1].active_item_ids),
            harness.session_runtime["active_item_ids"],
        )
        self.assertTrue(checkpoints[-1].durable_state["awaiting_continuation"])
        self.assertEqual(self.store.get_session(session_id).status, "initializing")
        self.assertEqual(len(harness.client.responses.calls), 1)

    def test_keyboard_interrupt_persists_balanced_outputs_without_replay(self):
        session_id = self.create_session(status="running")
        self.activate(session_id)
        tool_calls = [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "run_code",
                "arguments": '{"code":"side_effect()"}',
            },
            {
                "type": "function_call",
                "id": "fc_2",
                "call_id": "call_2",
                "name": "run_code",
                "arguments": '{"code":"must_not_run()"}',
            },
        ]
        harness.client = FakeClient(SimpleNamespace(output=tool_calls, output_text=""))

        with mock.patch.object(
            harness, "call_tool", side_effect=KeyboardInterrupt
        ) as call_tool:
            with self.assertRaises(KeyboardInterrupt):
                harness.run_agent("start", developer_instructions="scope")

        self.assertEqual(call_tool.call_count, 1)
        self.assertEqual(len(harness.client.responses.calls), 1)
        transcript = self.store.load_conversation(session_id)
        outputs = [
            item for item in transcript if item.get("type") == "function_call_output"
        ]
        self.assertEqual([item["call_id"] for item in outputs], ["call_1", "call_2"])
        current = json.loads(outputs[0]["output"])
        remaining = json.loads(outputs[1]["output"])
        self.assertEqual(
            current,
            {
                "ok": False,
                "interrupted": True,
                "not_executed": False,
                "error": current["error"],
            },
        )
        self.assertIn("side effects may have occurred", current["error"])
        self.assertEqual(
            remaining,
            {
                "ok": False,
                "interrupted": True,
                "not_executed": True,
                "error": remaining["error"],
            },
        )
        self.assertIn("not executed", remaining["error"])
        self.assertEqual(
            [record.origin for record in self.store.list_conversation_items(session_id)][-2:],
            ["tool", "tool"],
        )
        self.assertEqual(self.store.load_snapshot(session_id).active_messages, transcript)

    def test_keyboard_interrupt_during_model_call_preserves_request_checkpoint(self):
        session_id = self.create_session(status="running")
        self.activate(session_id)
        harness.client = llm_client.OpenAIResponsesClient(
            SimpleNamespace(
                responses=SimpleNamespace(
                    create=mock.Mock(side_effect=KeyboardInterrupt)
                )
            )
        )

        with self.assertRaises(KeyboardInterrupt):
            harness.run_agent("start", developer_instructions="scope")

        snapshot = self.store.load_snapshot(session_id)
        self.assertEqual(
            snapshot.active_messages,
            [
                {"role": "developer", "content": "scope"},
                {"role": "user", "content": "start"},
            ],
        )
        self.assertEqual(snapshot.full_history, snapshot.active_messages)

    def test_each_tool_result_is_checkpointed_before_the_next_tool_runs(self):
        session_id = self.create_session(status="running")
        self.activate(session_id)
        tool_calls = [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "run_code",
                "arguments": '{"code":"first()"}',
            },
            {
                "type": "function_call",
                "id": "fc_2",
                "call_id": "call_2",
                "name": "run_code",
                "arguments": '{"code":"second()"}',
            },
        ]
        final_item = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done", "annotations": []}],
        }
        harness.client = FakeClient(
            SimpleNamespace(output=tool_calls, output_text=""),
            SimpleNamespace(output=[final_item], output_text="done"),
        )
        seen_before_second = []

        def fake_call(tool_call):
            call_id = tool_call["call_id"]
            if call_id == "call_2":
                seen_before_second.extend(
                    item
                    for item in self.store.load_conversation(session_id)
                    if item.get("type") == "function_call_output"
                )
            return (
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"ok": True, "call_id": call_id}),
                },
                [],
            )

        with (
            mock.patch.object(harness, "call_tool", side_effect=fake_call),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            harness.run_agent("start", developer_instructions="scope")

        self.assertEqual(
            [item["call_id"] for item in seen_before_second], ["call_1"]
        )
        transcript = self.store.load_conversation(session_id)
        outputs = [
            item for item in transcript if item.get("type") == "function_call_output"
        ]
        self.assertEqual([item["call_id"] for item in outputs], ["call_1", "call_2"])
        second_request_outputs = [
            item
            for item in harness.client.responses.calls[1]["input"]
            if item.get("type") == "function_call_output"
        ]
        self.assertEqual(
            [item["call_id"] for item in second_request_outputs],
            ["call_1", "call_2"],
        )

    def test_non_interrupt_tool_exception_is_balanced_and_redacted(self):
        session_id = self.create_session(status="running")
        self.activate(session_id)
        harness.register_sensitive_value("tool-secret-value")
        tool_calls = [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "run_code",
                "arguments": '{"code":"explode()"}',
            },
            {
                "type": "function_call",
                "id": "fc_2",
                "call_id": "call_2",
                "name": "run_code",
                "arguments": '{"code":"must_not_run()"}',
            },
        ]
        final_item = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "recovered", "annotations": []}],
        }
        harness.client = FakeClient(
            SimpleNamespace(output=tool_calls, output_text=""),
            SimpleNamespace(output=[final_item], output_text="recovered"),
        )

        with (
            mock.patch.object(
                harness,
                "call_tool",
                side_effect=RuntimeError("failed with tool-secret-value"),
            ) as call_tool,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            harness.run_agent("start", developer_instructions="scope")

        self.assertEqual(call_tool.call_count, 1)
        outputs = [
            item
            for item in self.store.load_conversation(session_id)
            if item.get("type") == "function_call_output"
        ]
        self.assertEqual([item["call_id"] for item in outputs], ["call_1", "call_2"])
        first = json.loads(outputs[0]["output"])
        second = json.loads(outputs[1]["output"])
        self.assertFalse(first["not_executed"])
        self.assertTrue(second["not_executed"])
        self.assertIn("[REDACTED]", first["error"])
        self.assertNotIn("tool-secret-value", json.dumps(outputs))
        self.assertEqual(len(harness.client.responses.calls), 2)

    def test_compaction_record_and_restore_reproduce_active_context(self):
        session_id = self.create_session(status="running")
        self.activate(session_id)
        messages = [
            {"role": "developer", "content": "scope"},
            {"role": "user", "content": "initial reconstruction request"},
            {"role": "assistant", "content": "old motion-correction result"},
            {"role": "user", "content": "inspect old CTF result"},
            {"role": "assistant", "content": "J12 has a rare class"},
            {"role": "user", "content": "keep the class IDs"},
            {"role": "assistant", "content": "latest assistant context"},
            {"role": "user", "content": "latest user request"},
        ]
        harness.persist_appended_items(messages, messages, "bootstrap")
        before_ids = list(harness.session_runtime["active_item_ids"])
        expected_summary = structured_summary()

        compacted = harness.compact_messages(
            messages,
            force=True,
            reason="test_threshold",
            summary_func=lambda turns, previous: copy.deepcopy(expected_summary),
        )
        compacted_copy = copy.deepcopy(compacted)

        records = self.store.list_compactions(session_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].reason, "test_threshold")
        self.assertEqual(list(records[0].before_active_item_ids), before_ids)
        self.assertEqual(
            list(records[0].after_active_item_ids),
            harness.session_runtime["active_item_ids"],
        )
        self.assertIn(records[0].summary_item_id, records[0].after_active_item_ids)

        snapshot = self.store.load_snapshot(session_id)
        self.assertEqual(snapshot.active_messages, compacted_copy)
        self.activate(session_id)
        harness.restore_session_memory(snapshot)

        self.assertEqual(harness.agent_memory["active_messages"], compacted_copy)
        self.assertEqual(harness.agent_memory["last_summary"], expected_summary)
        self.assertEqual(
            harness.session_runtime["active_item_ids"],
            list(records[0].after_active_item_ids),
        )
        self.assertEqual(harness.agent_memory["full_history"], snapshot.full_history)

    def test_scope_is_created_once_reused_and_never_replaced_when_deleted(self):
        session_id = self.create_session(status="initializing")
        workflow = FakeWorkflow()
        marker = harness.session_scope_marker(session_id)

        first_manifest = harness.ensure_session_scope(workflow, self.store, session_id)

        self.assertEqual(
            workflow.project_creations,
            [
                {
                    "title": "Session reconstruction",
                    "project_dir": "/cryosparc/projects",
                    "desc": marker,
                }
            ],
        )
        self.assertEqual(
            workflow.workspace_creations,
            [{"title": "Automated Reconstruction", "desc": marker}],
        )
        self.assertEqual(first_manifest["project"]["project_uid"], "P1")
        self.assertEqual(first_manifest["project"]["workspace_uid"], "W1")

        second_manifest = harness.ensure_session_scope(workflow, self.store, session_id)
        self.assertEqual(second_manifest, first_manifest)
        self.assertEqual(len(workflow.project_creations), 1)
        self.assertEqual(len(workflow.workspace_creations), 1)

        del workflow.projects["P1"]
        with self.assertRaisesRegex(RuntimeError, "no replacement was created"):
            harness.ensure_session_scope(workflow, self.store, session_id)
        self.assertEqual(len(workflow.project_creations), 1)
        self.assertEqual(len(workflow.workspace_creations), 1)


    def test_scope_recovers_tagged_objects_after_remote_create_before_db_update(self):
        session_id = self.create_session(status="initializing")
        marker = harness.session_scope_marker(session_id)
        workflow = FakeWorkflow()
        project = FakeProject("P7", marker)
        project.workspaces["W8"] = FakeController("W8", marker)
        workflow.projects["P7"] = project

        manifest = harness.ensure_session_scope(workflow, self.store, session_id)

        self.assertEqual(workflow.project_creations, [])
        self.assertEqual(workflow.workspace_creations, [])
        self.assertEqual(manifest["project"]["project_uid"], "P7")
        self.assertEqual(manifest["project"]["workspace_uid"], "W8")
        session = self.store.get_session(session_id)
        self.assertEqual(session.project_uid, "P7")
        self.assertEqual(session.workspace_uid, "W8")

    def test_interrupted_project_creation_recovers_tagged_object_without_duplicate(self):
        session_id = self.create_session(status="initializing")

        class InterruptOnceWorkflow(FakeWorkflow):
            def __init__(self):
                super().__init__()
                self.interrupt_once = True

            def create_project(self, title, project_dir=None, desc=None):
                project = super().create_project(title, project_dir=project_dir, desc=desc)
                if self.interrupt_once:
                    self.interrupt_once = False
                    raise KeyboardInterrupt
                return project

        workflow = InterruptOnceWorkflow()
        with self.assertRaises(KeyboardInterrupt):
            harness.ensure_session_scope(workflow, self.store, session_id)

        self.assertIsNone(self.store.get_session(session_id).project_uid)
        manifest = harness.ensure_session_scope(workflow, self.store, session_id)

        self.assertEqual(len(workflow.project_creations), 1)
        self.assertEqual(len(workflow.workspace_creations), 1)
        self.assertEqual(manifest["project"]["project_uid"], "P1")
        self.assertEqual(manifest["project"]["workspace_uid"], "W1")

    def test_parser_exposes_resume_database_and_rejects_old_cli_surface(self):
        parser = harness.build_parser()
        args = parser.parse_args(
            ["--resume", "abc123", "--session-db", "/tmp/cryopilot-test.sqlite3"]
        )
        self.assertEqual(args.resume, "abc123")
        self.assertEqual(args.session_db, "/tmp/cryopilot-test.sqlite3")

        destinations = {action.dest for action in parser._actions}
        self.assertNotIn("prompt", destinations)
        self.assertNotIn("connect_cryosparc", destinations)
        self.assertNotIn("cryosparc_project", destinations)
        self.assertNotIn("cryosparc_workspace", destinations)
        with contextlib.redirect_stderr(io.StringIO()):
            for obsolete in (
                ["old positional prompt"],
                ["--connect-cryosparc"],
                ["--cryosparc-project", "P1"],
                ["--cryosparc-workspace", "W1"],
            ):
                with self.subTest(arguments=obsolete):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(obsolete)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            harness.print_session_resume(
                "session123",
                store=SimpleNamespace(path=Path("/tmp/session db.sqlite3")),
            )
        expected_db = Path("/tmp/session db.sqlite3").expanduser().resolve()
        self.assertIn(
            f"python reconstruction_agent/harness.py --session-db '{expected_db}' --resume session123",
            output.getvalue(),
        )

    def test_create_response_uses_native_stateless_replay_options_only_for_openai(self):
        native_client = FakeClient("native-response", reasoning_effort="high")
        harness.client = native_client

        result = harness.create_response(
            model="gpt-5.5",
            input=[],
            include=["message.input_image.image_url"],
        )

        self.assertEqual(result, "native-response")
        native_request = native_client.responses.calls[0]
        self.assertIs(native_request["store"], False)
        self.assertEqual(native_request["reasoning"], {"effort": "high"})
        self.assertEqual(
            native_request["include"],
            ["message.input_image.image_url", "reasoning.encrypted_content"],
        )

        compatible_client = FakeClient("compatible-response", native=False)
        harness.client = compatible_client
        result = harness.create_response(model="ollama-model", input=[])

        self.assertEqual(result, "compatible-response")
        compatible_request = compatible_client.responses.calls[0]
        self.assertNotIn("store", compatible_request)
        self.assertNotIn("include", compatible_request)
        self.assertNotIn("reasoning", compatible_request)

    def test_native_response_replay_omits_output_only_status_and_nulls(self):
        encrypted_content = (
            "opaque-ciphertext-sk-proj-this-is-not-an-api-key-1234567890"
        )
        source = [
            {
                "type": "reasoning",
                "id": "reasoning_1",
                "status": None,
                "content": None,
                "summary": [],
                "encrypted_content": encrypted_content,
            },
            {
                "type": "function_call",
                "id": "function_1",
                "call_id": "call_1",
                "name": "run_code",
                "arguments": "{}",
                "status": "completed",
                "namespace": "functions",
            },
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": "done",
                        "annotations": None,
                    }
                ],
            },
        ]
        original = copy.deepcopy(source)
        fake_client = FakeClient("response")
        harness.client = fake_client

        harness.create_response(model="gpt-5.5", input=source)

        self.assertEqual(source, original)
        replay = fake_client.responses.calls[0]["input"]
        self.assertNotIn("status", replay[0])
        self.assertNotIn("content", replay[0])
        self.assertNotIn("status", replay[1])
        self.assertNotIn("status", replay[2])
        self.assertNotIn("annotations", replay[2]["content"][0])
        self.assertEqual(replay[2]["phase"], "final_answer")
        self.assertEqual(replay[0]["encrypted_content"], encrypted_content)

    def test_secrets_are_removed_from_model_transcript_state_and_entire_database(self):
        environment_secret = "environment-token-should-never-persist"
        password = "cryosparc-password-should-never-persist"
        session_id = self.create_session(status="running")

        with mock.patch.dict(
            os.environ,
            {"CRYOPILOT_TEST_TOKEN": environment_secret},
            clear=False,
        ):
            self.activate(session_id)
            harness.register_sensitive_value(password)
            payload = {
                "type": "message",
                "role": "assistant",
                "api_key": environment_secret,
                "content": [
                    {
                        "type": "output_text",
                        "text": f"credentials: {environment_secret} and {password}",
                        "annotations": [],
                    }
                ],
            }
            harness.session_runtime["durable_state"] = {
                "note": f"never store {environment_secret}",
                "password": password,
            }
            harness.python_globals["session_state"] = harness.session_runtime[
                "durable_state"
            ]
            harness.persist_appended_items([payload], [payload], "model")

            class BrokenWorkflow:
                def list_jobs(self, limit=10000):
                    raise RuntimeError(
                        f"snapshot failed with {environment_secret} and {password}"
                    )

            harness.python_globals["csw"] = BrokenWorkflow()
            harness.persist_checkpoint(refresh_jobs=True)

            fake_client = FakeClient("response")
            harness.client = fake_client
            harness.create_response(
                model="gpt-5.5",
                input=[{"role": "user", "content": environment_secret}],
            )

        snapshot = self.store.load_snapshot(session_id)
        persisted = json.dumps(
            {
                "history": snapshot.full_history,
                "active": snapshot.active_messages,
                "state": snapshot.checkpoint.durable_state,
            },
            ensure_ascii=False,
        )
        model_request = json.dumps(fake_client.responses.calls[0], ensure_ascii=False)
        self.assertNotIn(environment_secret, persisted)
        self.assertNotIn(password, persisted)
        self.assertNotIn(environment_secret, model_request)
        self.assertIn("[REDACTED]", persisted)
        self.assertIn("[REDACTED]", model_request)

        database_values = []
        table_names = [
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not row[0].startswith("sqlite_")
        ]
        for table_name in table_names:
            for row in self.store.connection.execute(f"SELECT * FROM {table_name}"):
                database_values.extend(str(value) for value in tuple(row))
        entire_database = "\n".join(database_values)
        self.assertNotIn(environment_secret, entire_database)
        self.assertNotIn(password, entire_database)
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.store.path}{suffix}")
            if path.exists():
                raw = path.read_bytes()
                self.assertNotIn(environment_secret.encode(), raw)
                self.assertNotIn(password.encode(), raw)

    def test_short_secret_does_not_corrupt_scientific_history_or_protocol_fields(self):
        session_id = self.create_session(status="running")
        self.activate(session_id)
        harness.register_sensitive_value("a")
        payload = {
            "type": "message",
            "id": "a_message_id",
            "role": "assistant",
            "content": "a particle has a rare conformation",
        }

        harness.persist_appended_items([payload], [payload], "model")

        self.assertEqual(self.store.load_conversation(session_id), [payload])
        self.assertEqual(harness.redact_runtime_secrets("a\n"), "[REDACTED]\n")
        self.assertEqual(
            harness.redact_runtime_secrets("API key: a\n"),
            "API key: [REDACTED]\n",
        )
        self.assertEqual(
            harness.redact_runtime_secrets('{"password":"a"}'),
            '{"password":"[REDACTED]"}',
        )

    def test_shutdown_reconciles_a_checkpoint_committed_before_runtime_mirror(self):
        session_id = self.create_session(status="running")
        self.activate(session_id)
        bootstrap = [
            {"role": "developer", "content": "scope"},
            {"role": "user", "content": "start"},
        ]
        harness.persist_appended_items(bootstrap, bootstrap, "bootstrap")
        committed_output = {
            "type": "function_call_output",
            "call_id": "call_committed",
            "output": '{"ok":true}',
        }
        committed_ids, _ = self.store.append_items_and_checkpoint(
            session_id,
            "tool",
            [committed_output],
            active_item_ids=harness.session_runtime["active_item_ids"],
            durable_state=harness.session_runtime["durable_state"],
        )

        with contextlib.redirect_stdout(io.StringIO()):
            harness.finish_session(self.store, session_id, "interrupted")

        snapshot = self.store.load_snapshot(session_id)
        self.assertEqual(snapshot.active_messages[-1], committed_output)
        self.assertIn(committed_ids[0], snapshot.checkpoint.active_item_ids)

    def test_reset_agent_memory_is_not_exposed_to_the_repl(self):
        self.assertNotIn("reset_agent_memory", harness.python_globals)

    def test_shutdown_checkpoint_error_is_redacted_and_not_marked_completed(self):
        secret = "shutdown-secret-should-not-persist"
        session_id = self.create_session(status="running")
        self.activate(session_id)
        harness.register_sensitive_value(secret)

        with (
            mock.patch.object(
                harness,
                "persist_checkpoint",
                side_effect=RuntimeError(f"checkpoint failed with {secret}"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            harness.finish_session(self.store, session_id, "completed")

        session = self.store.get_session(session_id)
        self.assertEqual(session.status, "error")
        self.assertIn("[REDACTED]", session.last_error)
        self.assertNotIn(secret, session.last_error)

    def test_failed_reconnect_preserves_the_last_active_checkpoint(self):
        session_id = self.create_session(status="completed")
        original_messages = [
            {"role": "developer", "content": "scope"},
            {"role": "user", "content": "reconstruct"},
            {"role": "assistant", "content": "completed state"},
        ]
        original_ids = self.store.append_items(
            session_id, "bootstrap", original_messages
        )
        self.store.save_checkpoint(
            session_id,
            original_ids,
            durable_state={"known_jobs": [{"uid": "J1"}]},
        )
        harness.reset_agent_memory()

        with (
            mock.patch.object(harness, "configure_model_client"),
            mock.patch.object(
                harness,
                "collect_cryosparc_credentials",
                return_value={
                    "url": "https://cryosparc.example.test",
                    "email": "scientist@example.test",
                    "password": "reconnect-password",
                },
            ),
            mock.patch.object(
                harness,
                "connect_cryosparc",
                side_effect=RuntimeError("network unavailable"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                harness.main(
                    ["--session-db", str(self.store.path), "--resume", session_id]
                )

        snapshot = self.store.load_snapshot(session_id)
        self.assertEqual(snapshot.active_messages, original_messages)
        self.assertEqual(
            snapshot.checkpoint.active_item_ids, tuple(original_ids)
        )
        self.assertTrue(snapshot.checkpoint.durable_state["awaiting_continuation"])
        self.assertEqual(snapshot.session.status, "error")
        self.assertNotIn("reconnect-password", snapshot.session.last_error or "")

    def test_interrupted_continuation_prompt_remains_required_on_next_resume(self):
        session_id = self.create_session(status="completed")
        self.store.update_scope(session_id, project_uid="P9")
        self.store.update_scope(session_id, workspace_uid="W4")
        bound = bind_manifest_scope(
            manifest_template(),
            session_id=session_id,
            project_uid="P9",
            workspace_uid="W4",
        )
        self.store.update_manifest(session_id, bound)
        initial_messages = [
            {"role": "developer", "content": "scope"},
            {"role": "user", "content": "initial request"},
            {"role": "assistant", "content": "work completed"},
        ]
        initial_ids = self.store.append_items(
            session_id, "bootstrap", initial_messages
        )
        self.store.save_checkpoint(
            session_id,
            initial_ids,
            durable_state={"known_jobs": [], "awaiting_continuation": True},
        )
        marker = harness.session_scope_marker(session_id)
        workflow = FakeWorkflow()
        project = FakeProject("P9", marker)
        project.workspaces["W4"] = FakeController("W4", marker)
        workflow.projects["P9"] = project

        common_patches = (
            mock.patch.object(harness, "configure_model_client"),
            mock.patch.object(
                harness,
                "collect_cryosparc_credentials",
                return_value={
                    "url": "https://cryosparc.example.test",
                    "email": "scientist@example.test",
                    "password": "not-persisted",
                },
            ),
            mock.patch.object(harness, "connect_cryosparc", return_value=workflow),
        )
        with (
            common_patches[0],
            common_patches[1],
            common_patches[2],
            mock.patch.object(
                harness, "prompt_required", side_effect=KeyboardInterrupt
            ),
            mock.patch.object(harness, "run_agent") as run_agent,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = harness.main(
                ["--session-db", str(self.store.path), "--resume", session_id]
            )

        self.assertEqual(exit_code, 130)
        self.assertFalse(run_agent.called)
        interrupted = self.store.load_snapshot(session_id)
        self.assertEqual(interrupted.session.status, "interrupted")
        self.assertTrue(
            interrupted.checkpoint.durable_state["awaiting_continuation"]
        )

        captured = {}

        def fake_resume(prompt=None, developer_instructions=None, resume=False):
            captured["resume"] = resume
            captured["active"] = copy.deepcopy(
                harness.agent_memory["active_messages"]
            )
            captured["awaiting"] = harness.session_runtime["durable_state"].get(
                "awaiting_continuation"
            )

        with (
            mock.patch.object(harness, "configure_model_client"),
            mock.patch.object(
                harness,
                "collect_cryosparc_credentials",
                return_value={
                    "url": "https://cryosparc.example.test",
                    "email": "scientist@example.test",
                    "password": "not-persisted",
                },
            ),
            mock.patch.object(harness, "connect_cryosparc", return_value=workflow),
            mock.patch.object(
                harness,
                "prompt_required",
                return_value="Continue with 3D classification",
            ) as continuation_prompt,
            mock.patch.object(harness, "run_agent", side_effect=fake_resume),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = harness.main(
                ["--session-db", str(self.store.path), "--resume", session_id]
            )

        self.assertEqual(exit_code, 0)
        continuation_prompt.assert_called_once_with("Continuation request: ")
        self.assertTrue(captured["resume"])
        self.assertFalse(captured["awaiting"])
        self.assertEqual(
            captured["active"][-1],
            {"role": "user", "content": "Continue with 3D classification"},
        )

    def test_main_new_session_creates_scope_builds_prompt_and_completes(self):
        workflow = FakeWorkflow()
        manifest = manifest_template()
        captured = {}

        def fake_run_agent(prompt=None, developer_instructions=None, resume=False):
            harness.reset_agent_memory()
            captured.update(
                {
                    "prompt": prompt,
                    "developer": developer_instructions,
                    "resume": resume,
                }
            )

        with (
            mock.patch.object(harness, "configure_model_client"),
            mock.patch.object(
                harness,
                "collect_cryosparc_credentials",
                return_value={
                    "url": "https://cryosparc.example.test",
                    "email": "scientist@example.test",
                    "password": "not-persisted",
                },
            ),
            mock.patch.object(harness, "connect_cryosparc", return_value=workflow),
            mock.patch.object(harness, "collect_session_manifest", return_value=manifest),
            mock.patch.object(harness, "run_agent", side_effect=fake_run_agent),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = harness.main(
                ["--session-db", str(self.store.path), "--model", "gpt-5.5"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(workflow.project_creations), 1)
        self.assertEqual(len(workflow.workspace_creations), 1)
        sessions = self.store.connection.execute("SELECT id FROM sessions").fetchall()
        self.assertEqual(len(sessions), 1)
        session_id = sessions[0][0]
        session = self.store.get_session(session_id)
        self.assertEqual(session.status, "completed")
        self.assertEqual(session.project_uid, "P1")
        self.assertEqual(session.workspace_uid, "W1")
        self.assertNotIn("not-persisted", json.dumps(session.manifest))
        self.assertIn(f"Session ID: {session_id}", captured["developer"])
        self.assertIn("CryoSPARC project UID: P1", captured["developer"])
        self.assertIn('"user_goal": "Find a rare conformational state"', captured["prompt"])
        self.assertFalse(captured["resume"])

    def test_main_completed_resume_reuses_scope_and_appends_continuation(self):
        session_id = self.create_session(status="completed")
        self.store.update_scope(session_id, project_uid="P9")
        self.store.update_scope(session_id, workspace_uid="W4")
        bound = bind_manifest_scope(
            manifest_template(),
            session_id=session_id,
            project_uid="P9",
            workspace_uid="W4",
        )
        self.store.update_manifest(session_id, bound)
        initial_ids = self.store.append_items(
            session_id,
            "bootstrap",
            [
                {"role": "developer", "content": "scope"},
                {"role": "user", "content": "initial request"},
                {"role": "assistant", "content": "initial work completed"},
            ],
        )
        self.store.save_checkpoint(session_id, initial_ids, durable_state={"known_jobs": []})

        marker = harness.session_scope_marker(session_id)
        workflow = FakeWorkflow()
        project = FakeProject("P9", marker)
        workspace = FakeController("W4", marker)
        project.workspaces["W4"] = workspace
        workflow.projects["P9"] = project
        captured = {}

        def fake_resume(prompt=None, developer_instructions=None, resume=False):
            captured["resume"] = resume
            captured["active"] = copy.deepcopy(harness.agent_memory["active_messages"])

        with (
            mock.patch.object(harness, "configure_model_client"),
            mock.patch.object(
                harness,
                "collect_cryosparc_credentials",
                return_value={
                    "url": "https://cryosparc.example.test",
                    "email": "scientist@example.test",
                    "password": "not-persisted",
                },
            ),
            mock.patch.object(harness, "connect_cryosparc", return_value=workflow),
            mock.patch.object(harness, "prompt_required", return_value="Continue heterogeneity analysis"),
            mock.patch.object(harness, "run_agent", side_effect=fake_resume),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = harness.main(
                ["--session-db", str(self.store.path), "--resume", session_id]
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(captured["resume"])
        self.assertEqual(workflow.project_creations, [])
        self.assertEqual(workflow.workspace_creations, [])
        self.assertTrue(
            any(
                message.get("role") == "developer"
                and message.get("content", "").startswith("[SESSION RESUME]")
                for message in captured["active"]
            )
        )
        self.assertEqual(
            captured["active"][-1],
            {"role": "user", "content": "Continue heterogeneity analysis"},
        )
        self.assertEqual(self.store.get_session(session_id).status, "completed")
        self.assertNotIn("not-persisted", json.dumps(self.store.get_session(session_id).manifest))


if __name__ == "__main__":
    unittest.main()
