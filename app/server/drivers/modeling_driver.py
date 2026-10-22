#!/usr/bin/env python3
"""Long-lived driver for the cryo-EM modeling agent.

Runs inside the modeling agent's virtual environment, keeps conversation
memory in an openai-agents ``SQLiteSession`` so turns build on each other, and
streams reasoning summaries, tool calls, tool results, and the answer as JSON
events on stdout.

Usage:
    modeling_driver.py --session-id ID --memory-db PATH --workspace REL_DIR [--model M] [--effort E]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Any

DRIVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = DRIVER_DIR.parent.parent.parent
MODELING_ROOT = REPO_ROOT / "modeling_agent"
sys.path.insert(0, str(DRIVER_DIR))
sys.path.insert(0, str(MODELING_ROOT))

from protocol import CommandReader, Emitter, redirect_stdout_to_stderr  # noqa: E402

emitter = Emitter()
redirect_stdout_to_stderr()

from agents import Agent, ModelSettings, Runner, SQLiteSession  # noqa: E402
from agents.items import ToolCallItem, ToolCallOutputItem  # noqa: E402
from openai.types.responses import (  # noqa: E402
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseTextDeltaEvent,
)

from prompts import SYSTEM_PROMPT  # noqa: E402
from tools import ALL_TOOLS, ROOT  # noqa: E402

os.chdir(ROOT)

TRACE_LIMIT = 12_000


def status(state: str, detail: str = "") -> None:
    emitter.emit("status", state=state, detail=detail)


def log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _truncate(value: str, limit: int = TRACE_LIMIT) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} characters omitted]"


def _parse_arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _format_tool_output(output: Any) -> dict[str, Any]:
    """Return a JSON-safe view of a tool result, keeping images as data URLs."""

    if isinstance(output, str):
        return {"text": _truncate(output), "images": []}
    if isinstance(output, list):
        texts: list[str] = []
        images: list[str] = []
        for entry in output:
            entry_type = getattr(entry, "type", None)
            if entry_type == "image":
                url = getattr(entry, "image_url", None)
                if isinstance(url, str) and url.startswith("data:"):
                    images.append(url)
            elif entry_type == "text":
                texts.append(str(getattr(entry, "text", "")))
            else:
                texts.append(f"<{type(entry).__name__}>")
        return {"text": _truncate("\n".join(part for part in texts if part)), "images": images}
    return {"text": _truncate(str(output)), "images": []}


class EventTrace:
    def __init__(self) -> None:
        self.reasoning_buffer = ""
        self.answer_buffer = ""
        self.tool_names: dict[str, str] = {}
        self.answer_streamed = False

    def reasoning_delta(self, delta: str) -> None:
        self.reasoning_buffer += delta
        emitter.emit("delta", channel="reasoning", text=delta)

    def reasoning_done(self) -> None:
        text = self.reasoning_buffer.strip()
        self.reasoning_buffer = ""
        if text:
            emitter.emit("reasoning", text=text)

    def tool_called(self, item: ToolCallItem) -> None:
        self.reasoning_done()
        name = item.tool_name or "unknown_tool"
        if item.call_id:
            self.tool_names[item.call_id] = name
        arguments = getattr(item.raw_item, "arguments", None)
        if isinstance(item.raw_item, dict):
            arguments = item.raw_item.get("arguments", arguments)
        emitter.emit(
            "tool_call",
            name=name,
            call_id=item.call_id,
            arguments=_parse_arguments(arguments or "{}"),
        )

    def tool_output(self, item: ToolCallOutputItem) -> None:
        name = self.tool_names.get(item.call_id or "", "tool")
        emitter.emit("tool_output", name=name, call_id=item.call_id, **_format_tool_output(item.output))

    def answer_delta(self, delta: str) -> None:
        self.reasoning_done()
        self.answer_streamed = True
        self.answer_buffer += delta
        emitter.emit("delta", channel="answer", text=delta)

    def finish(self, final_output: str) -> None:
        self.reasoning_done()
        text = self.answer_buffer.strip() or final_output.strip()
        self.answer_buffer = ""
        emitter.emit("answer", text=text)


def build_agent(model: str, effort: str, workspace: str) -> Agent:
    instructions = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Agent root directory: {ROOT}\n"
        f"Project workspace (relative to the agent root): {workspace}/\n"
        "Read inputs from and write every artifact into that project workspace, "
        "using paths such as "
        f"'{workspace}/outputs/daq' and '{workspace}/outputs/render.png'."
    )
    return Agent(
        name="Cryo-EM modeling agent",
        instructions=instructions,
        model=model,
        model_settings=ModelSettings(reasoning={"effort": effort, "summary": "detailed"}),
        tools=ALL_TOOLS,
    )


async def run_turn(agent: Agent, session: SQLiteSession, request: str, max_turns: int) -> None:
    status("running", "Agent is working")
    trace = EventTrace()
    try:
        stream = Runner.run_streamed(agent, request, session=session, max_turns=max_turns)
        async for event in stream.stream_events():
            if event.type == "raw_response_event":
                data = event.data
                if isinstance(data, ResponseReasoningSummaryTextDeltaEvent):
                    trace.reasoning_delta(data.delta)
                elif isinstance(data, ResponseReasoningSummaryTextDoneEvent):
                    trace.reasoning_done()
                elif isinstance(data, ResponseTextDeltaEvent):
                    trace.answer_delta(data.delta)
            elif event.type == "run_item_stream_event":
                if event.name == "tool_called" and isinstance(event.item, ToolCallItem):
                    trace.tool_called(event.item)
                elif event.name == "tool_output" and isinstance(event.item, ToolCallOutputItem):
                    trace.tool_output(event.item)
        trace.finish(str(stream.final_output or ""))
    except asyncio.CancelledError:
        trace.reasoning_done()
        status("interrupted", "Stopped")
        emitter.emit("turn_end", outcome="interrupted")
        return
    except Exception as exc:
        log(traceback.format_exc())
        trace.reasoning_done()
        status("error", _truncate(str(exc), 800))
        emitter.emit("error", message=_truncate(str(exc), 800))
        emitter.emit("turn_end", outcome="error")
        return
    status("idle", "Waiting for your next message")
    emitter.emit("turn_end", outcome="completed")


async def main_async(args: argparse.Namespace) -> int:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        status("error", "OPENAI_API_KEY is not set")
        emitter.emit("error", message="OPENAI_API_KEY is not set")
        return 1
    workspace_dir = (ROOT / args.workspace).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agent = build_agent(args.model, args.effort, args.workspace)
    session = SQLiteSession(args.session_id, args.memory_db)
    reader = CommandReader()
    loop = asyncio.get_running_loop()
    # SIGINT is the server's "stop the current turn" signal; ignore it while idle.
    loop.add_signal_handler(signal.SIGINT, lambda: None)
    status("idle", "Ready")
    while True:
        command = await loop.run_in_executor(None, reader.next)
        if command is None or command.get("op") == "shutdown":
            log("Driver shutting down.")
            return 0
        if command.get("op") != "message":
            continue
        content = str(command.get("content") or "").strip()
        if not content:
            continue
        emitter.emit("user_turn_accepted")
        task = asyncio.create_task(run_turn(agent, session, content, args.max_turns))
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        try:
            await task
        finally:
            loop.add_signal_handler(signal.SIGINT, lambda: None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--memory-db", required=True)
    parser.add_argument("--workspace", required=True, help="Workspace directory relative to the modeling agent root.")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--max-turns", type=int, default=500)
    return parser.parse_args()


def main() -> int:
    try:
        return asyncio.run(main_async(parse_args()))
    except KeyboardInterrupt:
        status("stopped", "Driver interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
