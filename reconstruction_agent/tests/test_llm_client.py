import base64
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
from PIL import Image

import harness
import llm_client
from session_store import SessionStore


def block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def claude_message(*content, stop_reason="end_turn", transformations=(), stop_details=None):
    return SimpleNamespace(
        id="msg_1",
        model="claude-sonnet-5-5",
        content=list(content),
        stop_reason=stop_reason,
        stop_details=stop_details,
        input_transformations=list(transformations),
        usage=SimpleNamespace(
            input_tokens=10,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=5,
        ),
    )


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeAnthropic:
    """Records each request and returns (or raises) the queued replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **params):
        self.calls.append(copy.deepcopy(params))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return FakeStream(reply)


def png_data_url(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 80, 120)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class FakeOpenAI:
    """Records each Responses request."""

    def __init__(self):
        self.calls = []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **params):
        self.calls.append(copy.deepcopy(params))
        return SimpleNamespace(output=[], output_text="")


def image_url(mode, width, height):
    buffer = io.BytesIO()
    Image.new(mode, (width, height), 128).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def sent_image_size(call, index=-1):
    url = [item for item in call["input"] if isinstance(item.get("content"), list)][index]["content"][1]["image_url"]
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
        return url, image.size


def image_turn(url):
    return {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "Image loaded from J23_class_sheet.png."},
            {"type": "input_image", "image_url": url, "detail": "auto"},
        ],
    }


class OpenAIImageTests(unittest.TestCase):
    def test_patch_count_matches_openais_examples(self):
        self.assertEqual(llm_client.image_patches(4096, 512), 2048)
        self.assertEqual(llm_client.image_patches(2048, 2048), 4096)
        self.assertEqual(llm_client.image_patches(33, 1), 2)

    def test_a_class_sheet_over_the_patch_limit_is_shrunk_to_fit(self):
        sdk = FakeOpenAI()
        client = llm_client.OpenAIResponsesClient(sdk)
        sheet = image_url("L", 2048, 16384)
        self.assertGreater(llm_client.image_patches(2048, 16384), llm_client.OPENAI_IMAGE_MAX_PATCHES)
        history = [{"role": "user", "content": "Review the classes."}, image_turn(sheet)]

        client.create(model="gpt-6.1-sol", input=history)
        client.create(model="gpt-6.1-sol", input=history + [{"role": "user", "content": "Continue."}])

        first, size = sent_image_size(sdk.calls[0])
        again, _ = sent_image_size(sdk.calls[1])
        self.assertLessEqual(llm_client.image_patches(*size), llm_client.OPENAI_IMAGE_MAX_PATCHES)
        self.assertGreater(llm_client.image_patches(*size), 0.95 * llm_client.OPENAI_IMAGE_MAX_PATCHES)
        self.assertAlmostEqual(size[1] / size[0], 8, places=1)
        self.assertEqual(first, again)
        self.assertEqual(history[1]["content"][1]["image_url"], sheet)

    def test_images_within_the_limit_are_sent_unchanged(self):
        sdk = FakeOpenAI()
        small = image_url("RGB", 1600, 1200)

        llm_client.OpenAIResponsesClient(sdk).create(model="gpt-6.1-sol", input=[image_turn(small)])

        self.assertEqual(sent_image_size(sdk.calls[0])[0], small)

    def test_data_that_is_not_an_image_is_sent_unchanged(self):
        sdk = FakeOpenAI()
        url = "data:image/png;base64," + base64.b64encode(b"not an image").decode("ascii")

        llm_client.OpenAIResponsesClient(sdk).create(model="gpt-6.1-sol", input=[image_turn(url)])

        self.assertEqual(sdk.calls[0]["input"][0]["content"][1]["image_url"], url)


class AnthropicRequestTests(unittest.TestCase):
    def test_a_tool_output_list_becomes_text_and_images_in_the_tool_result(self):
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buffer, format="PNG")
        data = base64.b64encode(buffer.getvalue()).decode("ascii")
        client = llm_client.AnthropicMessagesClient(FakeAnthropic())

        _system, messages = client.messages(
            [
                {"role": "user", "content": "go"},
                {"type": "function_call", "call_id": "toolu_1", "name": "view_image", "arguments": "{}"},
                {
                    "type": "function_call_output",
                    "call_id": "toolu_1",
                    "output": [
                        {"type": "input_text", "text": "Loaded."},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{data}", "detail": "high"},
                    ],
                },
            ]
        )

        result = messages[-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["content"][0], {"type": "text", "text": "Loaded."})
        self.assertEqual(result["content"][1]["source"]["data"], data)

    def test_agent_turn_request(self):
        sdk = FakeAnthropic(claude_message(block("text", text="done")))
        client = llm_client.AnthropicMessagesClient(sdk, reasoning_effort="xhigh")

        client.create(
            model="claude-sonnet-5-5",
            input=[
                {"role": "developer", "content": "Scope: P1/W1."},
                {"role": "user", "content": "Process the data."},
            ],
            tools=harness.TOOLS,
            parallel_tool_calls=False,
        )

        params = sdk.calls[0]
        self.assertEqual(params["system"], "Scope: P1/W1.")
        self.assertEqual(
            params["messages"],
            [{"role": "user", "content": [{"type": "text", "text": "Process the data."}]}],
        )
        self.assertEqual(params["max_tokens"], llm_client.ANTHROPIC_MAX_TOKENS)
        self.assertEqual(params["output_config"], {"effort": "xhigh"})
        self.assertEqual(
            params["thinking"]["block_binding"], {"prefix_mismatch_behavior": "drop_block"}
        )
        self.assertIn("thinking-binding-controls-2026-08-01", params["betas"])
        self.assertEqual(
            params["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True}
        )
        self.assertEqual(params["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertEqual(
            [tool["name"] for tool in params["tools"]],
            ["run_code", "run_and_wait", "view_image"],
        )
        self.assertEqual(params["tools"][0]["input_schema"], harness.TOOLS[0]["parameters"])

    def test_compaction_request_uses_its_own_effort_and_json_schema(self):
        sdk = FakeAnthropic(claude_message(block("text", text="{}")))
        client = llm_client.AnthropicMessagesClient(sdk, reasoning_effort="xhigh")

        client.create(
            model="claude-sonnet-5-5",
            input=[
                {"role": "developer", "content": "Summarize."},
                {"role": "user", "content": "turns"},
            ],
            max_output_tokens=16000,
            reasoning={"effort": "minimal"},
            text={"format": {"type": "json_schema", "name": "x", "schema": {"type": "object"}, "strict": True}},
        )

        params = sdk.calls[0]
        self.assertEqual(params["max_tokens"], 16000)
        self.assertEqual(
            params["output_config"],
            {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}},
        )
        self.assertNotIn("cache_control", params)
        self.assertNotIn("tools", params)

    def test_history_becomes_alternating_turns(self):
        image_url = png_data_url(4, 4)
        items = [
            {"role": "developer", "content": "Scope."},
            {"role": "user", "content": "Start."},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Check CTF."}], "encrypted_content": "sig-1"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Looking.", "annotations": []}]},
            {"type": "function_call", "call_id": "toolu_1", "name": "view_image", "arguments": '{"image_path": "a.png"}'},
            {"type": "function_call_output", "call_id": "toolu_1", "output": '{"ok": true}'},
            {"role": "user", "content": [{"type": "input_text", "text": "Image loaded."}, {"type": "input_image", "image_url": image_url, "detail": "auto"}]},
            {"type": "reasoning", "summary": [], "encrypted_content": "opaque", "redacted": True},
            {"type": "function_call", "call_id": "toolu_2", "name": "run_code", "arguments": '{"code": "print(1)"}'},
            {"type": "function_call_output", "call_id": "toolu_2", "output": '{"ok": true, "stdout": "1"}'},
            {"role": "developer", "content": "[CONTEXT COMPACTION - REFERENCE ONLY]\nsummary"},
            {"role": "user", "content": "Continue."},
        ]
        client = llm_client.AnthropicMessagesClient(FakeAnthropic())

        system, messages = client.messages(items)

        self.assertEqual(system, "Scope.")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(
            messages[1]["content"],
            [
                {"type": "thinking", "thinking": "Check CTF.", "signature": "sig-1"},
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "id": "toolu_1", "name": "view_image", "input": {"image_path": "a.png"}},
            ],
        )
        tool_result = messages[2]["content"][0]
        self.assertEqual(len(messages[2]["content"]), 1)
        self.assertEqual(tool_result["tool_use_id"], "toolu_1")
        self.assertEqual(
            [part["type"] for part in tool_result["content"]], ["text", "text", "image"]
        )
        self.assertEqual(tool_result["content"][2]["source"]["media_type"], "image/png")
        self.assertEqual(
            messages[3]["content"][0], {"type": "redacted_thinking", "data": "opaque"}
        )
        self.assertEqual(
            [part["type"] for part in messages[4]["content"]], ["tool_result", "text", "text"]
        )
        self.assertTrue(messages[4]["content"][1]["text"].startswith("[CONTEXT COMPACTION"))

    def test_large_images_are_shrunk_the_same_way_every_time(self):
        client = llm_client.AnthropicMessagesClient(FakeAnthropic())
        small = png_data_url(300, 200)
        large = png_data_url(3000, 1000)

        self.assertEqual(client.image_block(small)["source"]["data"], small.split(",", 1)[1])
        first = client.image_block(large)["source"]
        again = llm_client.AnthropicMessagesClient(FakeAnthropic()).image_block(large)["source"]
        with Image.open(io.BytesIO(base64.b64decode(first["data"]))) as image:
            self.assertEqual(image.size, (2000, 667))
        self.assertEqual(first, again)


class AnthropicResponseTests(unittest.TestCase):
    def test_a_fallback_leaves_out_the_declined_attempts_thinking_and_calls(self):
        message = claude_message(
            block("thinking", thinking="Declined attempt.", signature="sig-a"),
            block("text", text="Partial text."),
            block("tool_use", id="toolu_1", name="run_code", input={}),
            block("fallback"),
            block("thinking", thinking="Fallback model.", signature="sig-b"),
            block("tool_use", id="toolu_2", name="run_code", input={}),
            stop_reason="tool_use",
        )

        output = llm_client.AnthropicMessagesClient(FakeAnthropic()).response(message).output

        self.assertEqual([item["type"] for item in output], ["message", "reasoning", "function_call"])
        self.assertEqual(output[1]["encrypted_content"], "sig-b")
        self.assertEqual(output[2]["call_id"], "toolu_2")

    def test_blocks_become_items_that_replay_unchanged(self):
        message = claude_message(
            block("thinking", thinking="Plan the next job.", signature="sig-a"),
            block("text", text="Running 2D classification."),
            block("tool_use", id="toolu_9", name="run_code", input={"code": "print('é')"}),
            stop_reason="tool_use",
        )
        client = llm_client.AnthropicMessagesClient(FakeAnthropic())

        response = client.response(message)

        self.assertEqual(response.status, "completed")
        self.assertEqual(response.output_text, "Running 2D classification.")
        self.assertEqual(
            [item["type"] for item in response.output], ["reasoning", "message", "function_call"]
        )
        self.assertEqual(json.loads(response.output[2]["arguments"]), {"code": "print('é')"})
        stored = harness.redact_persisted_value(llm_client.response_output_items(response))
        _system, messages = client.messages([{"role": "user", "content": "go"}, *stored])
        self.assertEqual(
            messages[1]["content"],
            [
                {"type": "thinking", "thinking": "Plan the next job.", "signature": "sig-a"},
                {"type": "text", "text": "Running 2D classification."},
                {"type": "tool_use", "id": "toolu_9", "name": "run_code", "input": {"code": "print('é')"}},
            ],
        )

    def test_a_call_cut_off_by_max_tokens_is_not_run(self):
        message = claude_message(
            block("text", text="Next I will"),
            block("tool_use", id="toolu_1", name="run_code", input={}),
            stop_reason="max_tokens",
        )
        response = llm_client.AnthropicMessagesClient(FakeAnthropic()).response(message)

        self.assertEqual(response.status, "incomplete")
        self.assertEqual(response.incomplete_details, {"reason": "max_output_tokens"})
        self.assertEqual([item["type"] for item in response.output], ["message"])

    def test_refusal_is_an_error(self):
        message = claude_message(
            stop_reason="refusal",
            stop_details=SimpleNamespace(category="bio", explanation="Declined."),
        )
        with self.assertRaisesRegex(llm_client.ModelRefusal, "category: bio"):
            llm_client.AnthropicMessagesClient(FakeAnthropic()).response(message)

    def test_dropped_thinking_blocks_are_traced(self):
        traces = []
        message = claude_message(
            block("text", text="ok"),
            transformations=[
                SimpleNamespace(type="thinking_dropped", path="messages.3.content.0", reason="prefix_binding_mismatch")
            ],
        )
        llm_client.AnthropicMessagesClient(FakeAnthropic(), trace=traces.append).response(message)

        self.assertIn("messages.3.content.0", traces[0])
        self.assertTrue(traces[1].startswith("[usage]"))

    def test_rejected_thinking_signature_is_resent_without_thinking(self):
        rejected = anthropic.BadRequestError(
            "messages.1.content.0: Invalid `signature` in `thinking` block",
            response=httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
            body=None,
        )
        sdk = FakeAnthropic(rejected, claude_message(block("text", text="ok")))
        client = llm_client.AnthropicMessagesClient(sdk)
        history = [
            {"role": "user", "content": "go"},
            {"type": "reasoning", "summary": [], "encrypted_content": "sig"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
            {"role": "user", "content": "more"},
        ]

        response = client.create(model="claude-sonnet-5-5", input=history)

        self.assertEqual(response.output_text, "ok")
        self.assertEqual(sdk.calls[0]["messages"][1]["content"][0]["type"], "thinking")
        self.assertEqual(sdk.calls[1]["messages"][1]["content"], [{"type": "text", "text": "hi"}])

    def test_context_window_errors_are_recognised(self):
        self.assertTrue(
            llm_client.is_context_window_error(
                RuntimeError("prompt is too long: 1050000 tokens > 1000000 maximum")
            )
        )
        self.assertFalse(llm_client.is_context_window_error(RuntimeError("overloaded")))


class AnthropicHarnessTests(unittest.TestCase):
    """The harness loop runs unchanged on Claude: a tool call, then an answer."""

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temp_directory.name) / "sessions.sqlite3")
        self.saved = {name: getattr(harness, name) for name in ("client", "MODEL", "TRACE_TOOLS", "CONTEXT_COMPACTION_ENABLED")}
        self.saved_memory = copy.deepcopy(harness.agent_memory)
        harness.TRACE_TOOLS = False
        harness.CONTEXT_COMPACTION_ENABLED = False

    def tearDown(self):
        harness.configure_session_runtime()
        for name, value in self.saved.items():
            setattr(harness, name, value)
        harness.agent_memory.clear()
        harness.agent_memory.update(self.saved_memory)
        self.store.close()
        self.temp_directory.cleanup()

    def test_compaction_leaves_out_the_kept_turns_thinking(self):
        harness.client = llm_client.AnthropicMessagesClient(FakeAnthropic())
        messages = [
            {"role": "developer", "content": "Scope."},
            {"role": "user", "content": "Start."},
        ]
        for index in range(12):
            messages += [
                {"type": "reasoning", "summary": [], "encrypted_content": f"sig-{index}"},
                {"type": "function_call", "call_id": f"toolu_{index}", "name": "run_code", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"toolu_{index}", "output": "x" * 4000},
            ]
        summary = json.loads(json.dumps(harness.empty_compaction_summary()))
        summary["current_goal"] = "Keep going."

        with contextlib.redirect_stderr(io.StringIO()):
            compacted = harness.compact_messages(
                messages, force=True, summary_func=lambda turns, previous: summary
            )

        self.assertTrue(harness.is_compaction_summary_message(compacted[2]))
        tail = compacted[3:]
        self.assertTrue(tail)
        self.assertNotIn("reasoning", [item.get("type") for item in tail])
        self.assertEqual(tail[0]["type"], "function_call")

    def test_tool_call_and_answer(self):
        session_id = self.store.create_session(
            provider="anthropic",
            model="claude-sonnet-5-5",
            reasoning={"effort": "high"},
            compact_config=harness.compact_configuration(),
            recursion_config={"max_depth": 1, "max_calls": 1, "max_model_turns": 1},
            cryosparc_url="http://localhost:61000",
            cryosparc_email="user@example.com",
            manifest={},
            status="running",
        )
        harness.configure_session_runtime(self.store, session_id)
        sdk = FakeAnthropic(
            claude_message(
                block("thinking", thinking="", signature="sig-1"),
                block("tool_use", id="toolu_1", name="run_code", input={"code": "print(6 * 7)"}),
                stop_reason="tool_use",
            ),
            claude_message(block("text", text="The answer is 42.")),
        )
        harness.client = llm_client.AnthropicMessagesClient(sdk, reasoning_effort="high")
        harness.MODEL = "claude-sonnet-5-5"

        with contextlib.redirect_stdout(io.StringIO()) as output:
            harness.run_agent("Compute 6 * 7.", developer_instructions="Scope.")

        self.assertIn("The answer is 42.", output.getvalue())
        second = sdk.calls[1]["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant", "user"])
        self.assertEqual(second[1]["content"][0], {"type": "thinking", "thinking": "", "signature": "sig-1"})
        result = json.loads(second[2]["content"][0]["content"])
        self.assertEqual(result["stdout"].strip(), "42")
        history = self.store.load_snapshot(session_id).full_history
        self.assertEqual(
            [item.get("type") or item.get("role") for item in history],
            ["developer", "user", "reasoning", "function_call", "function_call_output", "message"],
        )


if __name__ == "__main__":
    unittest.main()
