from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from agents.exceptions import MaxTurnsExceeded

import modeller


class FakeStream:
    def __init__(self, error=None, items=None, final="done"):
        self.error, self.items, self.final_output = error, items or [], final

    async def stream_events(self):
        if self.error:
            raise self.error
        if False:
            yield None

    def to_input_list(self):
        return self.items


class RetryTests(unittest.TestCase):
    def run_agent(self, streams):
        calls = []

        def fake_run_streamed(agent, conversation, max_turns):
            calls.append(conversation)
            return streams.pop(0)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
             patch.object(modeller.Runner, "run_streamed", side_effect=fake_run_streamed), \
             patch.object(modeller, "RETRY_BASE_SECONDS", 0):
            result = asyncio.run(modeller.run("model this map"))
        return result, calls

    def test_transient_error_resumes_the_same_conversation(self):
        history = [{"role": "user", "content": "model this map"}, {"type": "function_call_output", "output": "ok"}]
        streams = [FakeStream(error=RuntimeError("An error occurred while processing the request."), items=history),
                   FakeStream(final="finished")]
        result, calls = self.run_agent(streams)
        self.assertEqual(result, "finished")
        self.assertEqual(calls, ["model this map", history])

    def test_max_turns_is_not_retried(self):
        streams = [FakeStream(error=MaxTurnsExceeded("too many turns"))]
        with self.assertRaises(MaxTurnsExceeded):
            self.run_agent(streams)

    def test_gives_up_after_configured_retries(self):
        streams = [FakeStream(error=RuntimeError("server error"), items=["x"]) for _ in range(3)]
        with patch.object(modeller, "API_RETRIES", 2), self.assertRaisesRegex(RuntimeError, "server error"):
            self.run_agent(streams)


if __name__ == "__main__":
    unittest.main()
