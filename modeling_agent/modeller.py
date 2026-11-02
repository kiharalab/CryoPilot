#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent
try:
    from agents import Agent, ModelSettings, Runner, ToolOutputImage as _ToolOutputImageCheck
except (ModuleNotFoundError, ImportError):
    venv_python = APP_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists() and Path(sys.prefix).resolve() != (APP_ROOT / ".venv").resolve():
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise

sys.path.insert(0, str(APP_ROOT.parent / "reconstruction_agent"))

from agents.exceptions import (InputGuardrailTripwireTriggered, MaxTurnsExceeded, ModelRefusalError,
                               OutputGuardrailTripwireTriggered, UserError)
from agents.items import ToolCallItem, ToolCallOutputItem
from openai.types.responses import (
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseTextDeltaEvent,
)

from claude_model import agent_model, api_key_name
from llm_client import ModelRefusal
from prompts import SYSTEM_PROMPT
from tools import ALL_TOOLS, ROOT


MODEL = os.environ.get("MODELING_MODEL", "gpt-5.5")
TRACE_LIMIT = 1_000
# The agent plans its own modeling, rebuilding and refinement, which can take many tool calls.
MAX_TURNS = int(os.environ.get("MODELING_MAX_TURNS", "500"))
REASONING_EFFORT = os.environ.get("MODELING_REASONING_EFFORT", "high")
# Transient model-API failures resume the same conversation instead of ending the run.
API_RETRIES = int(os.environ.get("MODELING_API_RETRIES", "5"))
RETRY_BASE_SECONDS = float(os.environ.get("MODELING_RETRY_BASE_SECONDS", "30"))


def _truncate(value: str, limit: int = TRACE_LIMIT) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} characters omitted]"


def _format_arguments(raw_arguments: Any) -> str:
    if not isinstance(raw_arguments, str):
        return _truncate(str(raw_arguments))
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return _truncate(raw_arguments)
    return _truncate(json.dumps(parsed, ensure_ascii=False, separators=(", ", ": ")))


def _format_tool_output(output: Any) -> str:
    if isinstance(output, str):
        return _truncate(output)
    if isinstance(output, list):
        parts: list[str] = []
        for entry in output:
            entry_type = getattr(entry, "type", None)
            if entry_type == "image":
                parts.append("<image returned to model context>")
            elif entry_type == "text":
                parts.append(_truncate(str(getattr(entry, "text", "")), 700))
            else:
                parts.append(f"<{type(entry).__name__}>")
        return "; ".join(part for part in parts if part) or "<empty tool output>"
    return _truncate(str(output))


class TerminalTrace:
    def __init__(self) -> None:
        self.reasoning_open = False
        self.answer_open = False
        self.answer_streamed = False
        self.tool_names: dict[str, str] = {}

    def _close_reasoning(self) -> None:
        if self.reasoning_open:
            print()
            self.reasoning_open = False

    def _close_answer(self) -> None:
        if self.answer_open:
            print()
            self.answer_open = False

    def reasoning_delta(self, delta: str) -> None:
        self._close_answer()
        if not self.reasoning_open:
            print("\n[reasoning] ", end="", flush=True)
            self.reasoning_open = True
        print(delta, end="", flush=True)

    def reasoning_done(self) -> None:
        self._close_reasoning()

    def tool_called(self, item: ToolCallItem) -> None:
        self._close_reasoning()
        self._close_answer()
        name = item.tool_name or "unknown_tool"
        if item.call_id:
            self.tool_names[item.call_id] = name
        arguments = getattr(item.raw_item, "arguments", None)
        if isinstance(item.raw_item, dict):
            arguments = item.raw_item.get("arguments", arguments)
        print(f"\n[tool] {name}({_format_arguments(arguments or '{}')})", flush=True)

    def tool_output(self, item: ToolCallOutputItem) -> None:
        self._close_reasoning()
        self._close_answer()
        name = self.tool_names.get(item.call_id or "", "tool")
        print(f"[tool result] {name}: {_format_tool_output(item.output)}", flush=True)

    def answer_delta(self, delta: str) -> None:
        self._close_reasoning()
        if not self.answer_open:
            print("\n[answer] ", end="", flush=True)
            self.answer_open = True
        self.answer_streamed = True
        print(delta, end="", flush=True)

    def finish(self) -> None:
        self._close_reasoning()
        self._close_answer()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Terminal cryo-EM modeling and DAQ validation agent."
    )
    parser.add_argument("request", nargs="+", help="Natural-language modeling request.")
    return parser.parse_args()


NON_RETRYABLE = (MaxTurnsExceeded, UserError, ModelRefusalError, ModelRefusal, InputGuardrailTripwireTriggered,
                 OutputGuardrailTripwireTriggered, KeyboardInterrupt, asyncio.CancelledError)


async def _stream(stream, trace: "TerminalTrace") -> None:
    async for event in stream.stream_events():
        if event.type == "raw_response_event":
            if isinstance(event.data, ResponseReasoningSummaryTextDeltaEvent):
                trace.reasoning_delta(event.data.delta)
            elif isinstance(event.data, ResponseReasoningSummaryTextDoneEvent):
                trace.reasoning_done()
            elif isinstance(event.data, ResponseTextDeltaEvent):
                trace.answer_delta(event.data.delta)
        elif event.type == "run_item_stream_event":
            if event.name == "tool_called" and isinstance(event.item, ToolCallItem):
                trace.tool_called(event.item)
            elif event.name == "tool_output" and isinstance(event.item, ToolCallOutputItem):
                trace.tool_output(event.item)
        elif event.type == "agent_updated_stream_event":
            trace.finish()
            print(f"\n[agent] {event.new_agent.name}", flush=True)


async def run(request: str) -> str:
    key_name = api_key_name(MODEL)
    if not os.environ.get(key_name, "").strip():
        raise RuntimeError(f"{key_name} is not set.")
    agent = Agent(
        name="Cryo-EM modeling agent",
        instructions=SYSTEM_PROMPT,
        model=agent_model(MODEL),
        model_settings=ModelSettings(
            reasoning={"effort": REASONING_EFFORT, "summary": "concise"},
        ),
        tools=ALL_TOOLS,
    )
    trace = TerminalTrace()
    conversation: Any = request
    for attempt in range(API_RETRIES + 1):
        stream = Runner.run_streamed(agent, conversation, max_turns=MAX_TURNS)
        try:
            await _stream(stream, trace)
            break
        except NON_RETRYABLE:
            raise
        except Exception as exc:
            if attempt == API_RETRIES:
                raise
            conversation = stream.to_input_list()
            delay = min(600.0, RETRY_BASE_SECONDS * 2 ** attempt)
            trace.finish()
            print(f"\n[retry] model request failed ({type(exc).__name__}: {_truncate(str(exc), 300)}); "
                  f"resuming the same conversation ({len(conversation)} items) in {delay:.0f} s, "
                  f"attempt {attempt + 1} of {API_RETRIES}", flush=True)
            await asyncio.sleep(delay)

    trace.finish()
    final_output = str(stream.final_output)
    if not trace.answer_streamed:
        print(f"\n[answer] {final_output}", flush=True)
    return final_output


async def main() -> int:
    args = parse_args()
    request = " ".join(args.request).strip()
    print(f"Cryo-EM modeling agent · {MODEL}")
    print(f"Workspace: {ROOT}")
    print("─" * 72)
    try:
        await run(request)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
