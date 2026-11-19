"""The modeling driver's pause at the spending limit, with a stand-in model.

Run from modeling_agent/ in its environment:
    python -m pytest tests/test_run_gate.py -q
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "modeling_agent", ROOT / "reconstruction_agent", ROOT / "app" / "server" / "drivers"):
    sys.path.insert(0, str(path))

from agents import Agent, SQLiteSession  # noqa: E402
from agents.models.interface import Model  # noqa: E402
from openai.types.responses import Response, ResponseCompletedEvent, ResponseOutputMessage, ResponseOutputText  # noqa: E402
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails, ResponseUsage  # noqa: E402

import claude_model  # noqa: E402


class CountingModel(Model):
    """Answers at once and counts how many requests reached it."""

    def __init__(self):
        self.requests = 0

    async def get_response(self, *args, **kwargs):
        raise NotImplementedError

    async def stream_response(self, *args, **kwargs):
        self.requests += 1
        message = ResponseOutputMessage(
            id="msg_1",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text="Done.", annotations=[])],
        )
        yield ResponseCompletedEvent(
            type="response.completed",
            sequence_number=1,
            response=Response(
                id="resp_1",
                created_at=time.time(),
                model="claude-sonnet-5-5",
                object="response",
                output=[message],
                parallel_tool_calls=False,
                tool_choice="auto",
                tools=[],
                status="completed",
                usage=ResponseUsage(
                    input_tokens=1000,
                    input_tokens_details=InputTokensDetails(cached_tokens=600, cache_write_tokens=100, cache_write_long_tokens=100),
                    output_tokens=50,
                    output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
                    total_tokens=1050,
                ),
            ),
        )


@pytest.fixture
def driver(monkeypatch, tmp_path):
    pytest.importorskip("agents")
    module = importlib.import_module("modeling_driver")
    events = []
    monkeypatch.setattr(module.emitter, "emit", lambda event, **payload: events.append({"event": event, **payload}))
    module.gate.resume()
    module.test_events = events
    module.test_session = SQLiteSession("s1", str(tmp_path / "memory.sqlite3"))
    return module


def states(events):
    return [event["state"] for event in events if event["event"] == "status"]


def test_a_finished_response_reports_its_usage(driver):
    model = CountingModel()
    agent = Agent(name="test", instructions="x", model=model)
    asyncio.run(driver.run_turn(agent, driver.test_session, "hello", 3))
    usage = [event for event in driver.test_events if event["event"] == "usage"]
    assert usage == [
        {
            "event": "usage",
            "model": "claude-sonnet-5-5",
            "input_tokens": 300,
            "cached_tokens": 600,
            "cache_write_tokens": 0,
            "cache_write_long_tokens": 100,
            "output_tokens": 50,
        }
    ]
    assert states(driver.test_events) == ["running", "idle"]


def test_a_paused_run_sends_no_request_until_it_is_resumed(driver):
    model = CountingModel()
    agent = Agent(name="test", instructions="x", model=model)
    driver.gate.pause("Spending limit reached: about $5.00 of $5.00.")

    async def scenario():
        turn = asyncio.create_task(driver.run_turn(agent, driver.test_session, "hello", 3))
        await asyncio.sleep(0.6)
        assert model.requests == 0
        assert states(driver.test_events)[-1] == "paused"
        driver.gate.resume()
        await asyncio.wait_for(turn, 5)

    asyncio.run(scenario())
    assert model.requests == 1
    assert states(driver.test_events) == ["running", "paused", "running", "idle"]
    held = [event for event in driver.test_events if event.get("state") == "paused"]
    assert "Spending limit" in held[0]["detail"]


def test_stopping_a_paused_run_ends_the_turn(driver):
    model = CountingModel()
    agent = Agent(name="test", instructions="x", model=model)
    driver.gate.pause("limit")

    async def scenario():
        turn = asyncio.create_task(driver.run_turn(agent, driver.test_session, "hello", 3))
        await asyncio.sleep(0.6)
        turn.cancel()
        await asyncio.wait_for(turn, 5)

    asyncio.run(scenario())
    assert model.requests == 0
    assert states(driver.test_events)[-1] == "interrupted"
    assert any(event["event"] == "turn_end" and event["outcome"] == "interrupted" for event in driver.test_events)


def test_claude_usage_keeps_the_long_cache_writes_through_the_responses_shape():
    llm_client = importlib.import_module("llm_client")
    usage = claude_model.response_usage(
        SimpleNamespace(
            input_tokens=10,
            cache_read_input_tokens=500,
            cache_creation_input_tokens=40,
            cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=40, ephemeral_5m_input_tokens=0),
            output_tokens=7,
        )
    )
    assert llm_client.usage_record(usage, "claude-opus-5-5") == {
        "model": "claude-opus-5-5",
        "input_tokens": 10,
        "cached_tokens": 500,
        "cache_write_tokens": 0,
        "cache_write_long_tokens": 40,
        "output_tokens": 7,
    }
