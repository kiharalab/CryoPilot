import base64
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


def function_call(call_id, name, arguments, *, item_id=None):
    return {
        "type": "function_call",
        "id": item_id or f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }


def tool_response(*calls):
    return SimpleNamespace(output=list(calls), output_text="")


def final_response(text, *, item_id="msg_final"):
    return SimpleNamespace(
        output=[
            {
                "type": "message",
                "id": item_id,
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ],
        output_text=text,
    )


class FakeResponses:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("Unexpected extra model request")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def remaining(self):
        return len(self._responses)


class FakeClient:
    def __init__(self, *responses):
        self.responses = FakeResponses(responses)


class FakeController:
    def __init__(self, uid):
        self.uid = uid
        self.model = SimpleNamespace(uid=uid)


class FakeWorkflow:
    def __init__(self):
        self.projects = {
            "P1": {"controller": FakeController("P1"), "workspaces": {"W1": FakeController("W1")}},
            "P2": {"controller": FakeController("P2"), "workspaces": {"W2": FakeController("W2")}},
        }
        self.project = self.projects["P1"]["controller"]
        self.workspace = self.projects["P1"]["workspaces"]["W1"]
        self.use_log = []

    def use_project(self, uid):
        entry = self.projects[str(uid)]
        self.project = entry["controller"]
        self.workspace = None
        self.use_log.append(("project", str(uid)))
        return self.project

    def use_workspace(self, uid):
        project_uid = self.project.uid
        self.workspace = self.projects[project_uid]["workspaces"][str(uid)]
        self.use_log.append(("workspace", str(uid)))
        return self.workspace

    def list_jobs(self, limit=20):
        return []


EXPECTED_TOOLS = [
    {
        "type": "function",
        "name": "run_code",
        "description": (
            "Execute Python code immediately in a persistent REPL. Globals persist across calls, "
            "so variables like csw, jobs, paths, selected class lists, and cryoem_data_processing "
            "can be reused later. "
            "Use this for inspection, creating/configuring one cryoSPARC job, rendering sheets, "
            "printing summaries, and storing named variables."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "run_and_wait",
        "description": (
            "Poll a Python condition in the persistent REPL, then execute Python code once the condition is true. "
            "It does not time out. Use it to wait for a job status or file/path condition before taking the next step. "
            "Use an empty condition to execute immediately. For cryoSPARC jobs, prefer csw.wait_for_status(...) "
            "inside run_code unless the condition also handles terminal statuses; do not wait only for a transient "
            "state like status == 'waiting'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "condition": {
                    "type": "string",
                    "description": "Python expression evaluated repeatedly before running code. Use an empty string to run immediately.",
                },
                "poll_seconds": {"type": "number", "default": harness.WAIT_INTERVAL},
            },
            "required": ["code", "condition", "poll_seconds"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "view_image",
        "description": (
            "Load a local PNG/JPEG image into the conversation as visual context for the next model turn. "
            "Use this after exporting cryoSPARC plot PNGs or rendering a 2D class sheet. "
            "The tool returns metadata and appends the image; the model should then inspect the image visually."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string"},
                "prompt": {
                    "type": "string",
                    "description": "Optional text to include with the image.",
                },
            },
            "required": ["image_path"],
            "additionalProperties": False,
        },
        "strict": False,
    },
]


class RecursiveHarnessTests(unittest.TestCase):
    _HARNESS_GLOBAL_NAMES = (
        "client",
        "MODEL",
        "REASONING_EFFORT",
        "use_reasoning_effort",
        "use_native_openai",
        "CONTEXT_COMPACTION_ENABLED",
        "CONTEXT_WINDOW",
        "CONTEXT_COMPACTION_THRESHOLD",
        "CONTEXT_COMPACTION_TARGET_RATIO",
        "TRACE_TOOLS",
    )

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "sessions.sqlite3"
        self.store = SessionStore(self.database_path)
        self.saved_globals = {
            name: getattr(harness, name) for name in self._HARNESS_GLOBAL_NAMES
        }
        self.saved_memory = copy.deepcopy(harness.agent_memory)
        self.saved_runtime = {}
        for name, value in harness.session_runtime.items():
            self.saved_runtime[name] = value if name == "store" else copy.deepcopy(value)
        self.saved_python_globals = dict(harness.python_globals)
        self.frame_token = harness.current_agent_frame.set(None)
        self.invocation_token = harness.current_tool_invocation.set(None)
        self.wait_token = harness.evaluating_wait_condition.set(False)

        harness.MODEL = "test-model"
        harness.REASONING_EFFORT = ""
        harness.use_reasoning_effort = False
        harness.use_native_openai = True
        harness.CONTEXT_COMPACTION_ENABLED = False
        harness.TRACE_TOOLS = False
        self.workflow = FakeWorkflow()
        self.session_id = None

    def tearDown(self):
        harness.current_agent_frame.reset(self.frame_token)
        harness.current_tool_invocation.reset(self.invocation_token)
        harness.evaluating_wait_condition.reset(self.wait_token)
        for name, value in self.saved_globals.items():
            setattr(harness, name, value)
        harness.agent_memory.clear()
        harness.agent_memory.update(copy.deepcopy(self.saved_memory))
        harness.session_runtime.clear()
        harness.session_runtime.update(self.saved_runtime)
        harness.python_globals.clear()
        harness.python_globals.update(self.saved_python_globals)
        self.store.close()
        self.temp_directory.cleanup()

    def activate(self, *, max_depth=4, max_calls=16, max_model_turns=64):
        config = {
            "max_depth": max_depth,
            "max_calls": max_calls,
            "max_model_turns": max_model_turns,
        }
        self.session_id = self.store.create_session(
            provider="openai",
            model="test-model",
            reasoning={"effort": ""},
            compact_config={"enabled": False},
            recursion_config=config,
            cryosparc_url="https://cryosparc.example.test",
            cryosparc_email="scientist@example.test",
            manifest={
                "schema_version": 1,
                "project": {"project_uid": "P1", "workspace_uid": "W1"},
            },
            status="running",
        )
        self.store.update_scope(self.session_id, project_uid="P1")
        self.store.update_scope(self.session_id, workspace_uid="W1")
        harness.configure_session_runtime(self.store, self.session_id)
        harness.configure_recursion_config(config)
        harness.session_runtime["sensitive_values"] = []
        harness.python_globals["csw"] = self.workflow
        harness.python_globals["session_context"] = {
            "session": {"id": self.session_id},
            "project": {"project_uid": "P1", "workspace_uid": "W1"},
        }
        return self.session_id

    def run_root(self, responses, *, developer=None, prompt="root request"):
        self.assertIsNotNone(self.session_id)
        harness.client = FakeClient(*responses)
        trusted = developer or (
            "Immutable developer scope: CryoSPARC project P1 and workspace W1. "
            "Operate on one/few jobs only."
        )
        with contextlib.redirect_stdout(io.StringIO()):
            harness.run_agent(prompt, developer_instructions=trusted)
        self.assertEqual(harness.client.responses.remaining, 0)
        return harness.client, trusted

    def root_tool_output(self):
        transcript = self.store.load_conversation(self.session_id)
        outputs = [
            item for item in transcript if item.get("type") == "function_call_output"
        ]
        self.assertEqual(len(outputs), 1)
        return json.loads(outputs[0]["output"])

    def test_public_tool_schemas_remain_exactly_the_original_three(self):
        self.assertEqual(harness.TOOLS, EXPECTED_TOOLS)
        self.assertEqual(
            [tool["name"] for tool in harness.TOOLS],
            ["run_code", "run_and_wait", "view_image"],
        )
        self.assertNotIn("rlm", [tool["name"] for tool in harness.TOOLS])
        self.assertFalse(any(tool.get("parallel_tool_calls") for tool in harness.TOOLS))

    def test_two_level_rlm_nesting_shares_repl_restores_bindings_and_inherits_scope(self):
        self.activate()
        sentinel_messages = [{"sentinel": "restore-me"}]
        harness.python_globals["messages"] = sentinel_messages
        root_code = "\n".join(
            [
                "root_before_messages = [(m.get('role'), m.get('type')) for m in messages]",
                "root_frame_snapshot = [m.copy() for m in messages]",
                "shared_value = 'root-shared'",
                "root_result = rlm([{'role': 'user', 'content': 'solve child'}])",
                "root_after_messages = [(m.get('role'), m.get('type')) for m in messages]",
            ]
        )
        child_code = "\n".join(
            [
                "child_before_messages = [(m.get('role'), m.get('type')) for m in messages]",
                "child_frame_snapshot = [m.copy() for m in messages]",
                "child_saw_shared = shared_value",
                "grand_result = rlm.run([{'role': 'user', 'content': 'solve grandchild'}])",
                "child_after_messages = [(m.get('role'), m.get('type')) for m in messages]",
            ]
        )
        responses = [
            tool_response(function_call("root_call", "run_code", {"code": root_code})),
            tool_response(function_call("child_call", "run_code", {"code": child_code})),
            final_response("grandchild answer", item_id="msg_grandchild"),
            final_response("child answer", item_id="msg_child"),
            final_response("root answer", item_id="msg_root"),
        ]

        client, trusted = self.run_root(responses)

        self.assertEqual(harness.python_globals["shared_value"], "root-shared")
        self.assertEqual(harness.python_globals["child_saw_shared"], "root-shared")
        self.assertEqual(harness.python_globals["grand_result"], "grandchild answer")
        self.assertEqual(harness.python_globals["root_result"], "child answer")
        self.assertEqual(
            harness.python_globals["root_before_messages"],
            harness.python_globals["root_after_messages"],
        )
        self.assertEqual(
            harness.python_globals["child_before_messages"],
            harness.python_globals["child_after_messages"],
        )
        self.assertNotEqual(
            harness.python_globals["root_frame_snapshot"],
            harness.python_globals["child_frame_snapshot"],
        )
        self.assertIn("root request", json.dumps(harness.python_globals["root_frame_snapshot"]))
        self.assertIn("solve child", json.dumps(harness.python_globals["child_frame_snapshot"]))
        self.assertIs(harness.python_globals["messages"], sentinel_messages)
        self.assertIsNone(harness.current_agent_frame.get())
        self.assertIsNone(harness.current_tool_invocation.get())

        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual([call.depth for call in calls], [1, 2])
        self.assertEqual([call.status for call in calls], ["completed", "completed"])
        self.assertIsNone(calls[0].parent_call_id)
        self.assertEqual(calls[1].parent_call_id, calls[0].id)
        self.assertEqual([call.final_output for call in calls], ["child answer", "grandchild answer"])

        expected_child_instruction = trusted + "\n\n" + harness.RECURSIVE_CHILD_OVERLAY
        for request_index in (1, 2):
            child_input = client.responses.calls[request_index]["input"]
            self.assertEqual(child_input[0], {"role": "developer", "content": expected_child_instruction})
            self.assertIn("project P1", child_input[0]["content"])
            self.assertIn("workspace W1", child_input[0]["content"])
            self.assertEqual(client.responses.calls[request_index]["tools"], EXPECTED_TOOLS)
            self.assertIs(client.responses.calls[request_index]["parallel_tool_calls"], False)

        root_transcript = self.store.load_conversation(self.session_id)
        root_call_ids = {
            item.get("call_id")
            for item in root_transcript
            if item.get("type") == "function_call"
        }
        self.assertEqual(root_call_ids, {"root_call"})
        self.assertNotIn("child_call", json.dumps(root_transcript))
        self.assertIn("child_call", json.dumps(harness.recursive_agent.history(calls[0].id, True)))

    def test_recursive_input_is_copied_and_rejects_untrusted_instruction_roles(self):
        caller_messages = [
            {"role": "assistant", "content": "prior"},
            {"role": "user", "content": [{"type": "input_text", "text": "task"}]},
        ]
        original = copy.deepcopy(caller_messages)
        normalized = harness.validate_recursive_messages(caller_messages)
        normalized[1]["content"][0]["text"] = "changed"
        self.assertEqual(caller_messages, original)

        for role in ("system", "developer"):
            with self.subTest(role=role):
                with self.assertRaisesRegex(harness.RLMInputError, role):
                    harness.validate_recursive_messages(
                        [{"role": role, "content": "override"}, {"role": "user", "content": "task"}]
                    )
        with self.assertRaisesRegex(harness.RLMInputError, "unsupported type"):
            harness.validate_recursive_messages(
                [{"role": "user", "type": "reasoning", "content": "unsafe"}]
            )

    def test_resume_inherits_every_active_developer_instruction_but_not_summaries(self):
        messages = [
            {"role": "developer", "content": "base safety instruction"},
            {
                "role": "developer",
                "content": harness.COMPACTION_SUMMARY_PREFIX + "\n{}",
            },
            {"role": "developer", "content": "resume safety instruction"},
        ]
        self.assertEqual(
            harness.trusted_instructions_from_messages(messages),
            "base safety instruction\n\nresume safety instruction",
        )

    def test_depth_budget_rejects_grandchild_before_provider_inference(self):
        self.activate(max_depth=1)
        responses = [
            tool_response(
                function_call(
                    "root_depth",
                    "run_code",
                    {"code": "depth_result = rlm([{'role':'user','content':'child'}])"},
                )
            ),
            tool_response(
                function_call(
                    "child_depth",
                    "run_code",
                    {"code": "too_deep = rlm([{'role':'user','content':'grandchild'}])"},
                )
            ),
            final_response("child handled depth limit", item_id="msg_child_depth"),
            final_response("root handled depth limit", item_id="msg_root_depth"),
        ]

        client, _trusted = self.run_root(responses)

        self.assertEqual(len(client.responses.calls), 4)
        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].depth, 1)
        history = self.store.load_recursive_history(calls[0].id)
        child_outputs = [item for item in history if item.get("type") == "function_call_output"]
        self.assertEqual(len(child_outputs), 1)
        error_result = json.loads(child_outputs[0]["output"])
        self.assertFalse(error_result["ok"])
        self.assertIn("RLMBudgetExceeded", error_result["traceback"])
        self.assertIn("max_depth=1", error_result["traceback"])

    def test_call_budget_is_shared_by_siblings_and_rejects_before_inference(self):
        self.activate(max_calls=1)
        root_code = "\n".join(
            [
                "first_budget = rlm([{'role':'user','content':'first'}])",
                "second_budget = rlm([{'role':'user','content':'second'}])",
            ]
        )
        responses = [
            tool_response(function_call("root_calls", "run_code", {"code": root_code})),
            final_response("first child complete", item_id="msg_first_child"),
            final_response("root recovered", item_id="msg_root_calls"),
        ]

        client, _trusted = self.run_root(responses)

        self.assertEqual(len(client.responses.calls), 3)
        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].final_output, "first child complete")
        root_result = self.root_tool_output()
        self.assertFalse(root_result["ok"])
        self.assertIn("RLMBudgetExceeded", root_result["traceback"])
        self.assertIn("max_calls=1", root_result["traceback"])

    def test_model_turn_budget_counts_followup_turn_and_stops_before_request(self):
        self.activate(max_model_turns=1)
        responses = [
            tool_response(
                function_call(
                    "root_turn_budget",
                    "run_code",
                    {"code": "turn_budget = rlm([{'role':'user','content':'needs a tool'}])"},
                )
            ),
            tool_response(
                function_call(
                    "child_one_turn",
                    "run_code",
                    {"code": "model_turn_tool_ran = True"},
                )
            ),
            final_response("root recovered from turn budget", item_id="msg_root_turn_budget"),
        ]

        client, _trusted = self.run_root(responses)

        self.assertEqual(len(client.responses.calls), 3)
        self.assertTrue(harness.python_globals["model_turn_tool_ran"])
        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].status, "budget_exhausted")
        turns = self.store.list_recursive_model_turns(calls[0].id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].status, "completed")
        root_result = self.root_tool_output()
        self.assertFalse(root_result["ok"])
        self.assertIn("max_model_turns=1", root_result["traceback"])

    def test_forced_child_compaction_keeps_audit_items_and_replaces_only_active_middle(self):
        self.activate(max_model_turns=2)
        harness.CONTEXT_COMPACTION_ENABLED = True
        harness.CONTEXT_WINDOW = 100
        harness.CONTEXT_COMPACTION_THRESHOLD = 0.01
        harness.CONTEXT_COMPACTION_TARGET_RATIO = 0.20

        long_text = "historical-child-context-" * 100
        child_seed_messages = [
            {"role": "user", "content": "inherited head user"},
            {"role": "assistant", "content": "old-a-" + long_text},
            {"role": "user", "content": "old-b-" + long_text},
            {"role": "assistant", "content": "old-c-" + long_text},
            {"role": "user", "content": "old-d-" + long_text},
            {"role": "assistant", "content": "tail-a-" + long_text},
            {"role": "user", "content": "tail-b-" + long_text},
            {"role": "assistant", "content": "tail-c-" + long_text},
            {"role": "user", "content": "latest tail task"},
        ]
        harness.python_globals["child_seed_messages"] = child_seed_messages
        summary = harness.empty_compaction_summary()
        summary.update(
            {
                "current_goal": "Preserve the recursive child task.",
                "current_stage": "child-local compaction",
                "observations": ["durable raw compaction response marker"],
                "next_actions": ["Finish the child answer."],
            }
        )
        developer = (
            "Immutable child-compaction scope: CryoSPARC project P1 and workspace W1."
        )
        expected_bootstrap = [
            {
                "role": "developer",
                "content": developer + "\n\n" + harness.RECURSIVE_CHILD_OVERLAY,
            },
            *copy.deepcopy(child_seed_messages),
        ]
        tail_start = harness.find_tail_start(expected_bootstrap, 2)
        self.assertGreater(tail_start, 2)
        self.assertLess(tail_start, len(expected_bootstrap))
        responses = [
            tool_response(
                function_call(
                    "root_compaction",
                    "run_code",
                    {"code": "compacted_answer = rlm(child_seed_messages)"},
                )
            ),
            final_response(
                json.dumps(summary),
                item_id="msg_raw_recursive_compaction",
            ),
            final_response("compacted child complete", item_id="msg_compacted_child"),
            final_response("root compaction complete", item_id="msg_root_compaction"),
        ]

        client, _trusted = self.run_root(responses, developer=developer)

        self.assertEqual(len(client.responses.calls), 4)
        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].status, "completed")
        self.assertEqual(calls[0].metrics["model_turns"], 2)
        turns = self.store.list_recursive_model_turns(calls[0].id)
        self.assertEqual([turn.purpose for turn in turns], ["compaction", "agent"])
        self.assertEqual([turn.aggregate_ordinal for turn in turns], [0, 1])
        self.assertEqual([turn.status for turn in turns], ["completed", "completed"])

        records = self.store.list_recursive_items(calls[0].id)
        prompt_records = [
            record for record in records if record.origin == "compaction_prompt"
        ]
        self.assertEqual(len(prompt_records), 2)
        self.assertIn(
            "Conversation turns to compact",
            json.dumps([record.payload for record in prompt_records]),
        )
        raw_records = [
            record
            for record in records
            if isinstance(record.payload, dict)
            and record.payload.get("id") == "msg_raw_recursive_compaction"
        ]
        self.assertEqual(len(raw_records), 1)
        self.assertEqual(raw_records[0].origin, "compaction")
        self.assertIn(
            "durable raw compaction response marker",
            json.dumps(raw_records[0].payload),
        )

        agent_input = client.responses.calls[2]["input"]
        self.assertEqual(agent_input[:2], expected_bootstrap[:2])
        self.assertTrue(harness.is_compaction_summary_message(agent_input[2]))
        self.assertEqual(agent_input[3:], expected_bootstrap[tail_start:])
        self.assertLess(len(agent_input), len(expected_bootstrap))
        agent_input_json = json.dumps(agent_input)
        self.assertNotIn("msg_raw_recursive_compaction", agent_input_json)
        self.assertNotIn("Summarize prior recursive-agent turns", agent_input_json)

        items_by_id = {record.id: record.payload for record in records}
        durable_agent_input = [
            items_by_id[item_id] for item_id in turns[1].active_item_ids
        ]
        self.assertEqual(durable_agent_input, agent_input)

    def test_recursive_view_image_is_durable_but_isolated_from_root_replay(self):
        self.activate()
        image_path = Path(self.temp_directory.name) / "classes.png"
        image_bytes = b"ABCDEFGHI"
        encoded_payload = base64.b64encode(image_bytes).decode("ascii")
        harness.register_sensitive_value(encoded_payload)
        image_path.write_bytes(image_bytes)
        responses = [
            tool_response(
                function_call(
                    "root_image",
                    "run_code",
                    {"code": "image_answer = rlm([{'role':'user','content':'inspect image'}])"},
                )
            ),
            tool_response(
                function_call(
                    "child_image",
                    "view_image",
                    {"image_path": str(image_path), "prompt": "Inspect these classes."},
                )
            ),
            final_response("image inspected", item_id="msg_child_image"),
            final_response("root image complete", item_id="msg_root_image"),
        ]

        self.run_root(responses)

        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual(len(calls), 1)
        child_history = harness.recursive_agent.history(calls[0].id)
        image_messages = [
            item
            for item in child_history
            if item.get("role") == "user"
            and isinstance(item.get("content"), list)
            and any(
                isinstance(part, dict) and part.get("type") == "input_image"
                for part in item["content"]
            )
        ]
        self.assertEqual(len(image_messages), 1)
        encoded = image_messages[0]["content"][1]["image_url"]
        self.assertEqual(encoded, f"data:image/png;base64,{encoded_payload}")
        self.assertEqual(base64.b64decode(encoded.split(",", 1)[1]), image_bytes)

        root_transcript = self.store.load_conversation(self.session_id)
        root_json = json.dumps(root_transcript)
        self.assertNotIn("data:image/png;base64,", root_json)
        self.assertNotIn("child_image", root_json)
        self.assertIn("root_image", root_json)

    def test_run_and_wait_condition_cannot_start_recursive_work(self):
        frame = harness.AgentCallFrame(
            kind="root",
            messages=[{"role": "user", "content": "root"}],
            trusted_instructions="trusted",
        )
        tool_call = function_call("wait_parent", "run_and_wait", {})
        frame_token = harness.current_agent_frame.set(frame)
        try:
            with harness.active_tool_context(frame, tool_call):
                with self.assertRaisesRegex(harness.RLMUsageError, "run_and_wait condition"):
                    harness.run_and_wait(
                        "condition_code_ran = True",
                        "rlm([{'role':'user','content':'must not run'}])",
                        0,
                    )
        finally:
            harness.current_agent_frame.reset(frame_token)
        self.assertNotIn("condition_code_ran", harness.python_globals)

    def test_child_scope_switch_is_detected_and_cleanup_rebinds_expected_scope(self):
        self.activate()
        frame = harness.AgentCallFrame(
            kind="child",
            messages=[{"role": "user", "content": "bad switch"}],
            trusted_instructions="trusted",
            depth=1,
            recursive_call_id=123,
        )
        tool_call = function_call(
            "switch_scope",
            "run_code",
            {"code": "csw.use_project('P2')\ncsw.use_workspace('W2')"},
        )
        frame_token = harness.current_agent_frame.set(frame)
        try:
            with self.assertRaisesRegex(
                harness.RLMScopeViolation, "expected project/workspace P1/W1"
            ):
                harness.call_tool(tool_call)
        finally:
            harness.current_agent_frame.reset(frame_token)

        self.assertEqual(self.workflow.project.uid, "P1")
        self.assertEqual(self.workflow.workspace.uid, "W1")
        self.assertEqual(
            self.workflow.use_log[-2:],
            [("project", "P1"), ("workspace", "W1")],
        )

    def test_child_cannot_replace_or_delete_shared_csw_binding(self):
        self.activate()
        frame = harness.AgentCallFrame(
            kind="child",
            messages=[{"role": "user", "content": "tamper with csw"}],
            trusted_instructions="trusted",
            depth=1,
            recursive_call_id=124,
        )
        frame_token = harness.current_agent_frame.set(frame)
        try:
            for label, code in (
                ("replace", "csw = object()"),
                ("delete", "del csw"),
            ):
                with self.subTest(label=label):
                    harness.python_globals["csw"] = self.workflow
                    harness.session_runtime["bound_csw"] = self.workflow
                    with self.assertRaisesRegex(
                        harness.RLMScopeViolation, "replaced or removed"
                    ):
                        harness.call_tool(
                            function_call(
                                f"{label}_csw",
                                "run_code",
                                {"code": code},
                            )
                        )
                    self.assertIs(harness.python_globals["csw"], self.workflow)
                    self.assertEqual(self.workflow.project.uid, "P1")
                    self.assertEqual(self.workflow.workspace.uid, "W1")
        finally:
            harness.current_agent_frame.reset(frame_token)

    def test_child_cannot_redefine_expected_scope_through_session_context(self):
        self.activate()
        original_context = copy.deepcopy(harness.python_globals["session_context"])
        frame = harness.AgentCallFrame(
            kind="child",
            messages=[{"role": "user", "content": "mutate scope metadata"}],
            trusted_instructions="trusted",
            depth=1,
            recursive_call_id=125,
        )
        tool_call = function_call(
            "mutate_scope_context",
            "run_code",
            {
                "code": (
                    "session_context['project']['project_uid'] = 'P2'\n"
                    "session_context['project']['workspace_uid'] = 'W2'\n"
                    "csw.use_project('P2')\n"
                    "csw.use_workspace('W2')"
                )
            },
        )
        frame_token = harness.current_agent_frame.set(frame)
        try:
            with self.assertRaisesRegex(
                harness.RLMScopeViolation, "expected project/workspace P1/W1"
            ):
                harness.call_tool(tool_call)
        finally:
            harness.current_agent_frame.reset(frame_token)

        self.assertEqual(harness.python_globals["session_context"], original_context)
        self.assertEqual(self.workflow.project.uid, "P1")
        self.assertEqual(self.workflow.workspace.uid, "W1")

    def test_scope_cleanup_runs_when_scope_switch_then_keyboard_interrupts(self):
        self.activate()
        frame = harness.AgentCallFrame(
            kind="child",
            messages=[{"role": "user", "content": "switch then interrupt"}],
            trusted_instructions="trusted",
            depth=1,
            recursive_call_id=125,
        )
        tool_call = function_call(
            "switch_then_interrupt",
            "run_code",
            {
                "code": (
                    "csw.use_project('P2')\n"
                    "csw.use_workspace('W2')\n"
                    "raise KeyboardInterrupt()"
                )
            },
        )
        frame_token = harness.current_agent_frame.set(frame)
        try:
            with self.assertRaises(KeyboardInterrupt):
                harness.call_tool(tool_call)
        finally:
            harness.current_agent_frame.reset(frame_token)

        self.assertIs(harness.python_globals["csw"], self.workflow)
        self.assertEqual(self.workflow.project.uid, "P1")
        self.assertEqual(self.workflow.workspace.uid, "W1")
        self.assertEqual(
            self.workflow.use_log[-2:],
            [("project", "P1"), ("workspace", "W1")],
        )

    def test_child_keyboard_interrupt_balances_child_and_root_protocols(self):
        self.activate()
        responses = [
            tool_response(
                function_call(
                    "root_interrupt",
                    "run_code",
                    {"code": "interrupt_result = rlm([{'role':'user','content':'interrupt'}])"},
                )
            ),
            tool_response(
                function_call(
                    "child_interrupt",
                    "run_code",
                    {"code": "raise KeyboardInterrupt()"},
                )
            ),
        ]
        harness.client = FakeClient(*responses)

        with self.assertRaises(KeyboardInterrupt):
            harness.run_agent("root request", developer_instructions="Scope P1/W1")

        self.assertEqual(harness.client.responses.remaining, 0)
        root_transcript = self.store.load_conversation(self.session_id)
        self.assertEqual(harness.unmatched_function_calls(root_transcript), [])
        self.assertEqual(
            [item["call_id"] for item in root_transcript if item.get("type") == "function_call"],
            ["root_interrupt"],
        )
        root_outputs = [
            item for item in root_transcript if item.get("type") == "function_call_output"
        ]
        self.assertEqual([item["call_id"] for item in root_outputs], ["root_interrupt"])
        self.assertTrue(json.loads(root_outputs[0]["output"])["interrupted"])
        self.assertEqual(
            len(harness.session_runtime["active_item_ids"]),
            len(harness.agent_memory["active_messages"]),
        )

        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].status, "interrupted")
        child_history = self.store.load_recursive_history(calls[0].id)
        self.assertEqual(harness.unmatched_function_calls(child_history), [])
        child_outputs = [
            item for item in child_history if item.get("type") == "function_call_output"
        ]
        self.assertEqual([item["call_id"] for item in child_outputs], ["child_interrupt"])
        self.assertTrue(json.loads(child_outputs[0]["output"])["interrupted"])
        self.assertIsNone(harness.current_agent_frame.get())
        self.assertIsNone(harness.current_tool_invocation.get())

    def test_provider_error_and_recursive_messages_are_redacted_without_misaligning_root(self):
        self.activate()
        secret = "recursive-provider-secret-9f8e7d6c"
        harness.register_sensitive_value(secret)
        harness.python_globals["secret_value"] = secret
        responses = [
            tool_response(
                function_call(
                    "root_provider_error",
                    "run_code",
                    {
                        "code": (
                            "provider_result = rlm([{'role':'user',"
                            "'content': secret_value}])"
                        )
                    },
                )
            ),
            RuntimeError(f"provider failed while handling {secret}"),
            final_response("root recovered safely", item_id="msg_root_provider_error"),
        ]

        client, _trusted = self.run_root(responses)

        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].status, "failed")
        turns = self.store.list_recursive_model_turns(calls[0].id)
        self.assertEqual([turn.status for turn in turns], ["failed"])
        root_transcript = self.store.load_conversation(self.session_id)
        self.assertEqual(harness.unmatched_function_calls(root_transcript), [])
        root_result = self.root_tool_output()
        self.assertFalse(root_result["ok"])
        self.assertIn("RLMProviderError", root_result["traceback"])

        persisted_and_sent = {
            "root": root_transcript,
            "calls": [vars(call) for call in calls],
            "turns": [vars(turn) for turn in turns],
            "recursive": self.store.load_recursive_history(calls[0].id),
            "requests": client.responses.calls,
        }
        serialized = json.dumps(persisted_and_sent, default=str)
        self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)

        self.store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for path in (
            self.database_path,
            Path(str(self.database_path) + "-wal"),
            Path(str(self.database_path) + "-shm"),
        ):
            if path.exists():
                self.assertNotIn(secret.encode(), path.read_bytes())

    def test_child_answer_is_not_reported_as_success_if_trace_finalization_fails(self):
        self.activate()
        responses = [
            tool_response(
                function_call(
                    "root_finalize_failure",
                    "run_code",
                    {
                        "code": (
                            "finalize_result = rlm([{'role':'user',"
                            "'content':'finish durably'}])"
                        )
                    },
                )
            ),
            final_response("child answer that must not escape", item_id="msg_child_finalize"),
            final_response("root observed durable failure", item_id="msg_root_finalize"),
        ]
        harness.client = FakeClient(*responses)

        with mock.patch.object(
            self.store,
            "finalize_recursive_call",
            side_effect=OSError("simulated SQLite finalize failure"),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                harness.run_agent(
                    "root request", developer_instructions="Scope P1/W1"
                )

        result = self.root_tool_output()
        self.assertFalse(result["ok"])
        self.assertIn("RLMProviderError", result["traceback"])
        self.assertNotIn("finalize_result", harness.python_globals)
        calls = self.store.list_recursive_calls(self.session_id)
        self.assertEqual([call.status for call in calls], ["running"])


if __name__ == "__main__":
    unittest.main()
