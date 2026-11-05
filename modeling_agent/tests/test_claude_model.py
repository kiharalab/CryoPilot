from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "reconstruction_agent"))

import anthropic
import httpx2
from agents import Agent, ModelSettings, Runner, SQLiteSession, ToolOutputImage, ToolOutputText, function_tool
from agents.items import ToolCallItem, ToolCallOutputItem
from openai.types.responses import ResponseReasoningSummaryTextDeltaEvent, ResponseTextDeltaEvent
from PIL import Image

import claude_model
from claude_model import ClaudeModel, agent_model, api_key_name, leave_out_oldest_images
from llm_client import ModelRefusal


def png_url(size=(4, 4)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def message(*content, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        id="msg_1",
        model="claude-sonnet-5-5",
        content=list(content),
        stop_reason=stop_reason,
        stop_details=stop_details,
        input_transformations=None,
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=20
        ),
    )


def stream_events(reply):
    for item in reply.content:
        yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type=item.type))
        if item.type == "thinking":
            yield SimpleNamespace(
                type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking=item.thinking)
            )
        elif item.type == "text":
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=item.text))
        yield SimpleNamespace(type="content_block_stop")


class FakeStream:
    def __init__(self, reply):
        self.reply = reply

    async def __aenter__(self):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for event in stream_events(self.reply):
            yield event

    async def get_final_message(self):
        return self.reply


class FakeSdk:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **params):
        self.requests.append(copy.deepcopy(params))
        return FakeStream(self.replies.pop(0))


@function_tool
def render(path: str) -> list[ToolOutputText | ToolOutputImage]:
    """Render a map.

    Args:
        path: The map.
    """
    return [ToolOutputText(text=f"Rendered {path}."), ToolOutputImage(image_url=png_url(), detail="high")]


async def run_turn(agent, session, request):
    events = {"reasoning": [], "answer": [], "tools": [], "outputs": []}
    stream = Runner.run_streamed(agent, request, session=session, max_turns=5)
    async for event in stream.stream_events():
        if event.type == "raw_response_event":
            if isinstance(event.data, ResponseReasoningSummaryTextDeltaEvent):
                events["reasoning"].append(event.data.delta)
            elif isinstance(event.data, ResponseTextDeltaEvent):
                events["answer"].append(event.data.delta)
        elif event.type == "run_item_stream_event":
            if event.name == "tool_called" and isinstance(event.item, ToolCallItem):
                events["tools"].append(event.item.tool_name)
            elif event.name == "tool_output" and isinstance(event.item, ToolCallOutputItem):
                events["outputs"].append(event.item.output)
    return stream.final_output, events


class ConversationTests(unittest.TestCase):
    def test_a_session_runs_tools_and_replays_its_history_unchanged(self):
        sdk = FakeSdk(
            message(
                block("thinking", thinking="Render it first.", signature="sig-a"),
                block("tool_use", id="toolu_1", name="render", input={"path": "map.mrc"}),
                stop_reason="tool_use",
            ),
            message(block("thinking", thinking="It fits.", signature="sig-b"), block("text", text="The map fits.")),
            message(block("text", text="Second answer.")),
        )
        agent = Agent(
            name="Modeling",
            instructions="You build models.",
            model=ClaudeModel("claude-sonnet-5-5", sdk=sdk),
            model_settings=ModelSettings(reasoning={"effort": "minimal", "summary": "detailed"}),
            tools=[render],
        )

        async def conversation(path):
            session = SQLiteSession("s1", path)
            first = await run_turn(agent, session, "Check the map.")
            second = await run_turn(agent, session, "And now?")
            return first, second

        with tempfile.TemporaryDirectory() as folder:
            (final, events), (second_final, _) = asyncio.run(conversation(str(Path(folder) / "memory.sqlite3")))

        self.assertEqual(final, "The map fits.")
        self.assertEqual(second_final, "Second answer.")
        self.assertEqual(events["reasoning"], ["Render it first.", "It fits."])
        self.assertEqual(events["answer"], ["The map fits."])
        self.assertEqual(events["tools"], ["render"])
        self.assertEqual(len(events["outputs"]), 1)

        first, with_result, next_turn = sdk.requests
        self.assertEqual(first["system"], "You build models.")
        self.assertEqual(first["output_config"], {"effort": "low"})
        self.assertEqual(first["tools"][0]["name"], "render")
        self.assertIn("path", first["tools"][0]["input_schema"]["properties"])
        self.assertEqual(first["fallbacks"], "default")
        self.assertIn(claude_model.FALLBACK_BETA, first["betas"])

        assistant, tool_turn = with_result["messages"][1:3]
        self.assertEqual(assistant["content"][0]["signature"], "sig-a")
        self.assertEqual(assistant["content"][1]["id"], "toolu_1")
        result = tool_turn["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["content"][0], {"type": "text", "text": "Rendered map.mrc."})
        self.assertEqual(result["content"][1]["type"], "image")

        # Claude reads a thinking block only if the conversation before it is unchanged.
        self.assertEqual(next_turn["messages"][: len(with_result["messages"])], with_result["messages"])
        self.assertEqual(next_turn["messages"][-2]["content"][0]["signature"], "sig-b")
        self.assertEqual(next_turn["messages"][-1]["content"], [{"type": "text", "text": "And now?"}])

    def test_a_run_without_streaming_returns_the_answer_and_usage(self):
        agent = Agent(name="Modeling", model=ClaudeModel("claude-sonnet-5-5", sdk=FakeSdk(message(block("text", text="Done.")))))
        result = asyncio.run(Runner.run(agent, "Go."))
        self.assertEqual(result.final_output, "Done.")
        usage = result.context_wrapper.usage
        self.assertEqual((usage.input_tokens, usage.output_tokens), (130, 5))
        self.assertEqual(usage.input_tokens_details.cached_tokens, 100)

    def test_a_refusal_ends_the_turn_with_the_reason(self):
        details = SimpleNamespace(category="bio", explanation="Declined.")
        agent = Agent(
            name="Modeling",
            model=ClaudeModel("claude-sonnet-5-5", sdk=FakeSdk(message(stop_reason="refusal", stop_details=details))),
        )
        with self.assertRaisesRegex(ModelRefusal, "category: bio"):
            asyncio.run(run_turn(agent, None, "Go."))

    def test_a_rejected_thinking_block_resends_once_without_thinking(self):
        rejected = anthropic.BadRequestError(
            "Invalid signature in thinking block",
            response=httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com")),
            body=None,
        )
        sdk = FakeSdk(rejected, message(block("text", text="Done.")))
        history = [
            {"role": "user", "content": "Go."},
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "old"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "Ok.", "annotations": []}]},
            {"role": "user", "content": "Next."},
        ]
        agent = Agent(name="Modeling", model=ClaudeModel("claude-sonnet-5-5", sdk=sdk))

        final, _ = asyncio.run(run_turn(agent, None, history))

        self.assertEqual(final, "Done.")
        self.assertEqual(sdk.requests[0]["messages"][1]["content"][0]["type"], "thinking")
        self.assertEqual([part["type"] for part in sdk.requests[1]["messages"][1]["content"]], ["text"])

    def test_a_model_without_fallbacks_sends_none(self):
        sdk = FakeSdk(message(block("text", text="Done.")))
        agent = Agent(name="Modeling", model=ClaudeModel("claude-haiku-5-5", sdk=sdk))
        asyncio.run(run_turn(agent, None, "Go."))
        self.assertNotIn("fallbacks", sdk.requests[0])
        self.assertNotIn(claude_model.FALLBACK_BETA, sdk.requests[0]["betas"])


class ChoosingTheModelTests(unittest.TestCase):
    def test_a_claude_name_selects_claude_and_its_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "made-up"}), patch.object(
            claude_model, "set_tracing_disabled"
        ) as tracing:
            model = agent_model("claude-opus-5-5")
        self.assertIsInstance(model, ClaudeModel)
        tracing.assert_called_once_with(True)
        self.assertEqual(api_key_name("claude-opus-5-5"), "ANTHROPIC_API_KEY")

    def test_other_names_stay_with_openai(self):
        with patch.object(claude_model, "set_tracing_disabled") as tracing:
            self.assertEqual(agent_model("gpt-5.5"), "gpt-5.5")
        tracing.assert_not_called()
        self.assertEqual(api_key_name("gpt-5.5"), "OPENAI_API_KEY")


def image_turn(size):
    source = {"type": "base64", "media_type": "image/png", "data": "x" * size}
    return {"role": "user", "content": [{"type": "tool_result", "content": [{"type": "image", "source": source}]}]}


def kept(messages):
    return [block["content"][0]["type"] == "image" for turn in messages for block in turn["content"]]


class ImageLimitTests(unittest.TestCase):
    def test_images_under_the_limit_stay(self):
        messages = [image_turn(claude_model.IMAGES_LIMIT // 2), image_turn(claude_model.IMAGES_LIMIT // 2)]
        self.assertEqual(leave_out_oldest_images(messages), 0)
        self.assertEqual(kept(messages), [True, True])

    def test_the_oldest_images_go_first_and_are_replaced_by_a_note(self):
        size = 6 * 1024 * 1024
        messages = [image_turn(size) for _ in range(5)]
        self.assertEqual(leave_out_oldest_images(messages), 1)
        self.assertEqual(kept(messages), [False, True, True, True, True])
        self.assertEqual(messages[0]["content"][0]["content"][0]["text"], claude_model.IMAGE_LEFT_OUT)

    def test_images_are_left_out_in_steps_so_the_conversation_stays_the_same(self):
        size = 1024 * 1024
        counts = []
        for turns in range(25, 42):
            messages = [image_turn(size) for _ in range(turns)]
            counts.append(leave_out_oldest_images(messages))
        self.assertEqual(sorted(set(counts)), [8, 16, 24][: len(set(counts))])
        self.assertEqual(counts, sorted(counts))


class ImageSizeTests(unittest.TestCase):
    def test_a_large_render_is_made_smaller_and_stays_the_same_on_replay(self):
        noise = Image.frombytes("RGB", (1500, 1500), random.Random(1).randbytes(1500 * 1500 * 3))
        buffer = io.BytesIO()
        noise.save(buffer, format="PNG")
        data = base64.b64encode(buffer.getvalue()).decode("ascii")
        client = ClaudeModel("claude-sonnet-5-5", sdk=FakeSdk()).client

        first = client.fit_image("image/png", data)
        second = client.fit_image("image/png", data)

        self.assertEqual(first, second)
        self.assertEqual(first[0], "image/jpeg")
        self.assertLess(len(first[1]), len(data))

    def test_a_small_image_is_sent_as_it_is(self):
        data = png_url().split(",", 1)[1]
        client = ClaudeModel("claude-sonnet-5-5", sdk=FakeSdk()).client
        self.assertEqual(client.fit_image("image/png", data), ("image/png", data))


DRIVER_SCRIPT = """
import asyncio, sys
sys.path[:0] = [{tests!r}, {drivers!r}]
from agents import Agent, SQLiteSession
from test_claude_model import FakeSdk, block, message, render
import modeling_driver
from claude_model import ClaudeModel

sdk = FakeSdk(
    message(block("text", text="Rendering the map."), block("tool_use", id="toolu_1", name="render", input={{"path": "a.mrc"}}), stop_reason="tool_use"),
    message(block("text", text="It shows one particle.")),
)
agent = Agent(name="test", instructions="Help.", model=ClaudeModel("claude-sonnet-5-5", sdk=sdk), tools=[render])
asyncio.run(modeling_driver.run_turn(agent, SQLiteSession("test"), "Render it.", 5))
"""


class DriverEventTests(unittest.TestCase):
    def test_text_before_a_tool_call_is_not_part_of_the_final_answer(self):
        tests = Path(__file__).resolve().parent
        drivers = tests.parents[1] / "app" / "server" / "drivers"
        with tempfile.TemporaryDirectory() as sandbox:
            completed = subprocess.run(
                [sys.executable, "-c", DRIVER_SCRIPT.format(tests=str(tests), drivers=str(drivers))],
                env={**os.environ, "MODELING_AGENT_SANDBOX": sandbox, "ANTHROPIC_API_KEY": "test"},
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
        shown = [(event["event"], event.get("text")) for event in events if event["event"] in ("answer", "tool_call", "tool_output")]
        self.assertEqual(
            [(name, text) for name, text in shown if name != "tool_output"],
            [("answer", "Rendering the map."), ("tool_call", None), ("answer", "It shows one particle.")],
        )


if __name__ == "__main__":
    unittest.main()
