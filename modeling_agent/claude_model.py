"""Claude models for the modeling agent.

The agent runs on the OpenAI Agents SDK, which keeps a conversation as OpenAI
Responses items and reads a model's answer as Responses stream events. A
``claude-…`` model name selects ``ClaudeModel``, an Agents SDK model that sends
each request through the Anthropic Messages API. ``AnthropicMessagesClient``
translates the items, as it does for the reconstruction agent.
"""

from __future__ import annotations

import math
import sys
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import anthropic
from agents import FunctionTool, ModelSettings, set_tracing_disabled
from agents.exceptions import UserError
from agents.items import ModelResponse
from agents.models.fake_id import FAKE_RESPONSES_ID
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseReasoningItem,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from llm_client import ANTHROPIC_MAX_RETRIES, AnthropicMessagesClient, provider_for_model, without_thinking

# Claude's safety classifiers can decline a request, and cryo-EM samples include
# viral proteins and toxins. With fallbacks the API runs a declined request on
# another model instead of ending the turn. These models accept the setting.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# A session replays every image the tools returned, and renders of maps and
# models average 0.65 MB. A smaller limit than the reconstruction agent's keeps
# more of them inside the API's request size limit.
IMAGE_MAX_BYTES = 400 * 1024
# The API rejects a request body over about 32 MB, which would leave the session
# stuck. Past this many bytes of images, the oldest ones are left out in steps,
# so that most requests resend the same conversation and read it from the cache.
IMAGES_LIMIT = 24 * 1024 * 1024
IMAGES_STEP = 8 * 1024 * 1024
IMAGE_LEFT_OUT = (
    "[An earlier image was left out to keep the conversation within the size limit. "
    "Load it again with view_image if you need it.]"
)


def agent_model(name: str) -> str | Model:
    """The model to give an Agent: the name for OpenAI, a ClaudeModel for Claude."""

    if provider_for_model(name) != "anthropic":
        return name
    # The SDK exports its traces to OpenAI; a Claude conversation stays out of them.
    set_tracing_disabled(True)
    return ClaudeModel(name)


def api_key_name(name: str) -> str:
    """The environment variable that holds the API key for this model."""

    return "ANTHROPIC_API_KEY" if provider_for_model(name) == "anthropic" else "OPENAI_API_KEY"


def leave_out_oldest_images(messages: list[dict[str, Any]]) -> int:
    """Replaces, in place, the oldest images past IMAGES_LIMIT. Returns how many.

    The images left out depend only on the size of the conversation, and change
    once per IMAGES_STEP of new images.
    """

    images = []
    for message in messages:
        block_lists = [message["content"]]
        block_lists += [
            block["content"]
            for block in message["content"]
            if block.get("type") == "tool_result" and isinstance(block["content"], list)
        ]
        for blocks in block_lists:
            for index, block in enumerate(blocks):
                if block.get("type") == "image" and block["source"].get("type") == "base64":
                    images.append((blocks, index, len(block["source"]["data"])))
    total = sum(size for _, _, size in images)
    if total <= IMAGES_LIMIT:
        return 0
    allowance = math.ceil((total - IMAGES_LIMIT) / IMAGES_STEP) * IMAGES_STEP
    left_out = 0
    for blocks, index, size in images:
        if size > allowance:
            break
        allowance -= size
        blocks[index] = {"type": "text", "text": IMAGE_LEFT_OUT}
        left_out += 1
    return left_out


def response_item(item: dict[str, Any]) -> Any:
    """A Responses output item, as the SDK expects it, from the translated dict."""

    if item["type"] == "function_call":
        return ResponseFunctionToolCall.model_validate(item)
    if item["type"] == "reasoning":
        return ResponseReasoningItem.model_validate({**item, "id": f"rs_{uuid4().hex}"})
    return ResponseOutputMessage.model_validate({**item, "id": f"msg_{uuid4().hex}", "status": "completed"})


def response_usage(usage: Any) -> ResponseUsage:
    cached = usage.cache_read_input_tokens or 0
    written = usage.cache_creation_input_tokens or 0
    # Claude counts cached input apart from input_tokens; OpenAI includes it.
    input_tokens = usage.input_tokens + cached + written
    return ResponseUsage(
        input_tokens=input_tokens,
        input_tokens_details=InputTokensDetails(cached_tokens=cached, cache_write_tokens=written),
        output_tokens=usage.output_tokens,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=input_tokens + usage.output_tokens,
    )


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class ClaudeModel(Model):
    def __init__(self, name: str, sdk: Any = None) -> None:
        self.name = name
        self.sdk = sdk or anthropic.AsyncAnthropic(max_retries=ANTHROPIC_MAX_RETRIES)
        self.client = AnthropicMessagesClient(self.sdk, trace=log, image_max_bytes=IMAGE_MAX_BYTES)

    def request(
        self,
        system_instructions: str | None,
        input: Any,
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
    ) -> dict[str, Any]:
        """The Messages API request for one model turn."""

        if output_schema is not None or handoffs:
            raise UserError("Claude models in the modeling agent do not support output schemas or handoffs.")
        for tool in tools:
            if not isinstance(tool, FunctionTool):
                raise UserError(f"Claude models in the modeling agent cannot use the tool {tool.name}.")
        items = [{"role": "user", "content": input}] if isinstance(input, str) else list(input)
        if system_instructions:
            items.insert(0, {"role": "developer", "content": system_instructions})
        reasoning = model_settings.reasoning
        params = self.client.messages_request(
            {
                "model": self.name,
                "input": items,
                "tools": [
                    {"name": tool.name, "description": tool.description, "parameters": tool.params_json_schema}
                    for tool in tools
                ],
                "reasoning": {"effort": reasoning.effort} if reasoning and reasoning.effort else None,
                "parallel_tool_calls": model_settings.parallel_tool_calls,
                "max_output_tokens": model_settings.max_tokens,
            }
        )
        left_out = leave_out_oldest_images(params["messages"])
        if left_out:
            log(f"[anthropic] Left out the {left_out} oldest images to stay within the request size limit.")
        if self.name in FALLBACK_MODELS:
            params["fallbacks"] = "default"
            params["betas"] = [*params["betas"], FALLBACK_BETA]
        return params

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ) -> ModelResponse:
        final = None
        async for event in self.stream_response(
            system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
            previous_response_id=previous_response_id, conversation_id=conversation_id, prompt=prompt,
        ):
            if isinstance(event, ResponseCompletedEvent):
                final = event.response
        usage = final.usage
        return ModelResponse(
            output=final.output,
            usage=Usage(
                requests=1,
                input_tokens=usage.input_tokens,
                input_tokens_details=usage.input_tokens_details,
                output_tokens=usage.output_tokens,
                output_tokens_details=usage.output_tokens_details,
                total_tokens=usage.total_tokens,
            ),
            response_id=final.id,
        )

    async def stream_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ) -> AsyncIterator[Any]:
        params = self.request(system_instructions, input, model_settings, tools, output_schema, handoffs)
        sequence = 0

        def number() -> int:
            nonlocal sequence
            sequence += 1
            return sequence

        message = None
        thinking_text = ""
        block_type = None
        summary_index = 0
        can_retry = True
        while message is None:
            try:
                async with self.sdk.beta.messages.stream(**params) as stream:
                    async for event in stream:
                        if event.type == "content_block_start":
                            block_type = event.content_block.type
                            thinking_text = ""
                        elif event.type == "content_block_delta" and event.delta.type == "thinking_delta":
                            thinking_text += event.delta.thinking
                            yield ResponseReasoningSummaryTextDeltaEvent(
                                delta=event.delta.thinking,
                                item_id=FAKE_RESPONSES_ID,
                                output_index=0,
                                summary_index=summary_index,
                                type="response.reasoning_summary_text.delta",
                                sequence_number=number(),
                            )
                        elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                            yield ResponseTextDeltaEvent(
                                content_index=0,
                                delta=event.delta.text,
                                item_id=FAKE_RESPONSES_ID,
                                logprobs=[],
                                output_index=0,
                                type="response.output_text.delta",
                                sequence_number=number(),
                            )
                        elif event.type == "content_block_stop" and block_type == "thinking":
                            yield ResponseReasoningSummaryTextDoneEvent(
                                item_id=FAKE_RESPONSES_ID,
                                output_index=0,
                                summary_index=summary_index,
                                text=thinking_text,
                                type="response.reasoning_summary_text.done",
                                sequence_number=number(),
                            )
                            summary_index += 1
                    message = await stream.get_final_message()
            except anthropic.BadRequestError as exc:
                # A thinking block the API cannot read; see AnthropicMessagesClient.create.
                text = str(exc)
                if not can_retry or "signature" not in text or "thinking" not in text:
                    raise
                can_retry = False
                log("[anthropic] An earlier thinking block was rejected; resending this request without earlier thinking.")
                params["messages"] = without_thinking(params["messages"])
        translated = self.client.response(message)
        output = [response_item(item) for item in translated.output]
        for index, item in enumerate(output):
            yield ResponseOutputItemDoneEvent(
                item=item,
                output_index=index,
                type="response.output_item.done",
                sequence_number=number(),
            )
        yield ResponseCompletedEvent(
            response=Response(
                id=message.id,
                created_at=time.time(),
                model=message.model,
                object="response",
                output=output,
                parallel_tool_calls=model_settings.parallel_tool_calls is not False,
                tool_choice="auto",
                tools=[],
                status="completed",
                usage=response_usage(message.usage),
            ),
            type="response.completed",
            sequence_number=number(),
        )
