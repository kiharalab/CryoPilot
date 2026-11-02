"""Model clients for the reconstruction agent.

The harness keeps its conversation as OpenAI Responses API items: messages,
reasoning items, function calls, and function call outputs. The session store,
context compaction, and the app's trace view all read that format. Each client
here takes a request in that shape and returns a response whose ``output`` holds
items in that shape, so the rest of the harness does not depend on the provider.

- ``OpenAIResponsesClient``: the OpenAI Responses API, or a Responses-compatible
  server such as Ollama.
- ``AnthropicMessagesClient``: Claude models through the Anthropic Messages API.
"""

import base64
import copy
import hashlib
import io
import json
import math
import os
from types import SimpleNamespace

import anthropic
from openai import OpenAI
from PIL import Image


PROVIDERS = ("openai", "anthropic", "ollama")
OLLAMA_BASE_URL = "http://localhost:11434/v1/"
OLLAMA_API_KEY = "ollama"
OLLAMA_MODEL = "gemma4:12b-mlx"
# GPT-6 and GPT-5.6 models read an image at its full size in 32 px patches,
# and reject an image of more than 30,000 patches instead of shrinking it.
OPENAI_IMAGE_PATCH = 32
OPENAI_IMAGE_MAX_PATCHES = 30_000
DEFAULT_MODELS = {
    "openai": os.environ.get("OPENAI_MODEL", "gpt-5.5"),
    "anthropic": os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
    "ollama": OLLAMA_MODEL,
}

# Claude needs max_tokens on every request, and thinking counts against it.
# Requests stream, so a long turn at high effort does not time out.
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "64000"))
# Runs go on for hours unattended; retry overloaded and rate-limited requests
# with backoff for longer than the SDK's default of two retries.
ANTHROPIC_MAX_RETRIES = 6
# Claude checks each replayed thinking block against the conversation that
# produced it. Compaction keeps the latest turns verbatim after a summary, and
# redaction can change earlier text, so those blocks fail the check. With
# drop_block the API drops the failing blocks instead of rejecting the request.
ANTHROPIC_BETAS = ["thinking-binding-controls-2026-08-01"]
ANTHROPIC_THINKING = {
    "type": "adaptive",
    # Summaries of the reasoning, and the notes the model writes between tool
    # calls, which come back as thinking blocks.
    "display": "summarized",
    "block_binding": {"prefix_mismatch_behavior": "drop_block"},
}
# Agent turns resend a growing conversation that only ever has turns added at
# the end, so the cache serves almost all of it. CryoSPARC jobs often run
# longer than the default 5-minute cache lifetime.
ANTHROPIC_CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
# Once a request carries more than 20 images, the API rejects any image larger
# than 2000 px on a side. Claude reads at most 2576 px of an image anyway.
ANTHROPIC_IMAGE_MAX_EDGE = 2000
ANTHROPIC_IMAGE_MAX_BYTES = 7 * 1024 * 1024
ANTHROPIC_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
# Claude's effort levels for the reasoning efforts the harness accepts. Claude
# always thinks, so "none" and "minimal" become its lowest level.
ANTHROPIC_EFFORTS = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
CONTEXT_WINDOW_ERROR_TEXT = (
    "context window",
    "context length",
    "maximum context",
    "too many tokens",
    "token limit",
    "context_length_exceeded",
    "prompt is too long",
)


class ModelRefusal(RuntimeError):
    """The model declined the request."""


def provider_for_model(model):
    """The provider that serves a model name: Claude models run on Anthropic."""

    return "anthropic" if str(model or "").startswith("claude-") else "openai"


def connect(provider, reasoning_effort=None, trace=None):
    """A client for a saved session's provider. API keys come from the environment."""

    if provider == "openai":
        return OpenAIResponsesClient(OpenAI(), reasoning_effort=reasoning_effort)
    if provider == "ollama":
        return OpenAIResponsesClient(
            OpenAI(base_url=OLLAMA_BASE_URL, api_key=OLLAMA_API_KEY), native=False
        )
    if provider == "anthropic":
        return AnthropicMessagesClient(
            anthropic.Anthropic(max_retries=ANTHROPIC_MAX_RETRIES),
            reasoning_effort=reasoning_effort,
            trace=trace,
        )
    raise ValueError(f"Unsupported saved provider: {provider!r}")


def is_context_window_error(exc):
    text = str(exc).lower()
    return any(needle in text for needle in CONTEXT_WINDOW_ERROR_TEXT)


def response_output_items(response):
    items = getattr(response, "output", None) or []
    output = []
    for item in items:
        if isinstance(item, dict):
            output.append(copy.deepcopy(item))
        elif hasattr(item, "model_dump"):
            output.append(item.model_dump(mode="json", exclude_none=False))
        else:
            output.append(json.loads(json.dumps(item, default=str)))
    text = getattr(response, "output_text", None)
    has_output_text = any(
        isinstance(item, dict)
        and item.get("type") == "message"
        and any(
            isinstance(part, dict) and part.get("type") == "output_text"
            for part in (item.get("content") or [])
        )
        for item in output
    )
    if text and not has_output_text:
        # Some Responses-compatible providers expose assistant text only on
        # output_text, even when response.output contains reasoning metadata.
        # Normalize it into the provider-neutral replay envelope without
        # replacing or mutating the provider's exact output items.
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": str(text),
                        "annotations": [],
                    }
                ],
            }
        )
    return output


def response_text(response):
    text = getattr(response, "output_text", None)
    if text:
        return text

    pieces = []
    for item in response_output_items(response):
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                pieces.append(part.get("text", ""))
    return "".join(pieces)


# ------------------------------------------------------------------- OpenAI
def normalize_native_response_input(value, *, top_level=False):
    """Build an API-compatible replay copy without changing durable history."""

    if isinstance(value, dict):
        item_type = value.get("type") if top_level else None
        normalized = {}
        for key, item in value.items():
            if item is None:
                continue
            if (
                top_level
                and key == "status"
                and item_type in {"message", "reasoning", "function_call"}
            ):
                continue
            normalized[key] = normalize_native_response_input(item)
        return normalized
    if isinstance(value, list):
        return [
            normalize_native_response_input(item, top_level=isinstance(item, dict))
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            normalize_native_response_input(item, top_level=isinstance(item, dict))
            for item in value
        ]
    return copy.deepcopy(value)


def image_patches(width, height):
    """The number of patches an OpenAI model reads an image of this size in."""
    return math.ceil(width / OPENAI_IMAGE_PATCH) * math.ceil(height / OPENAI_IMAGE_PATCH)


class OpenAIResponsesClient:
    """The OpenAI Responses API.

    With ``native=False`` it talks to a Responses-compatible server such as
    Ollama, which takes no reasoning settings, structured output, or OpenAI's
    stateless replay options. For OpenAI, images over the patch limit are
    shrunk until they fit, because the API rejects them.
    """

    provider = "openai"

    def __init__(self, sdk, *, native=True, reasoning_effort=None):
        self.sdk = sdk
        self.native = native
        self.reasoning_effort = reasoning_effort
        self._images = {}

    def create(self, **request):
        if not self.native:
            request.pop("reasoning", None)
            request.pop("text", None)
            return self.sdk.responses.create(**request)
        if "input" in request:
            request["input"] = normalize_native_response_input(request["input"])
            self.fit_images(request["input"])
        if self.reasoning_effort:
            request.setdefault("reasoning", {"effort": self.reasoning_effort})
        request.setdefault("store", False)
        include = list(request.pop("include", []) or [])
        if "reasoning.encrypted_content" not in include:
            include.append("reasoning.encrypted_content")
        request["include"] = include
        return self.sdk.responses.create(**request)

    def fit_images(self, value):
        """Fits, in place, every image in a request copy to the patch limit."""
        if isinstance(value, list):
            for item in value:
                self.fit_images(item)
        elif isinstance(value, dict):
            url = value.get("image_url")
            if value.get("type") == "input_image" and isinstance(url, str) and url.startswith("data:"):
                value["image_url"] = self.fit_image(url)
                return
            for item in value.values():
                self.fit_images(item)

    def fit_image(self, url):
        """The image as an OpenAI model accepts it: at most
        OPENAI_IMAGE_MAX_PATCHES patches, shrunk no more than that needs. The
        result is the same every time for the same image, so earlier turns are
        sent unchanged."""

        key = hashlib.sha256(url.encode("ascii")).hexdigest()
        if key in self._images:
            return self._images[key]
        header, _, data = url.partition(",")
        fitted = url
        image = None
        if header.endswith(";base64"):
            try:
                image = Image.open(io.BytesIO(base64.b64decode(data)))
            except (OSError, ValueError):
                pass  # not an image PIL reads: sent as it is, for the API to judge
        if image is not None:
            with image:
                width, height = image.size
                patches = image_patches(width, height)
                if patches > OPENAI_IMAGE_MAX_PATCHES:
                    scale = math.sqrt(OPENAI_IMAGE_MAX_PATCHES / patches)
                    size = (max(1, int(width * scale)), max(1, int(height * scale)))
                    while image_patches(*size) > OPENAI_IMAGE_MAX_PATCHES:
                        scale *= 0.99
                        size = (max(1, int(width * scale)), max(1, int(height * scale)))
                    if image.mode not in {"RGB", "RGBA", "L", "LA"}:
                        image = image.convert("RGBA")
                    buffer = io.BytesIO()
                    image.resize(size, Image.Resampling.LANCZOS).save(buffer, format="PNG")
                    fitted = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        self._images[key] = fitted
        return fitted


# ---------------------------------------------------------------- Anthropic
class AnthropicMessagesClient:
    """Claude models through the Anthropic Messages API.

    Requests and responses are translated to and from Responses items:

    - The leading developer message is the system prompt. Later developer
      messages (compaction summaries, resume notes) become user text, because
      a mid-conversation system message must be followed by an assistant turn.
    - A ``thinking`` block becomes a ``reasoning`` item: the summary text in
      ``summary`` and the signature in ``encrypted_content``, which redaction
      leaves alone. A ``redacted_thinking`` block keeps its data there too.
    - ``tool_use`` and ``tool_result`` blocks become ``function_call`` and
      ``function_call_output`` items. An image that ``view_image`` loads goes
      inside the tool result it belongs to.
    """

    provider = "anthropic"

    def __init__(self, sdk, *, reasoning_effort=None, trace=None, image_max_bytes=ANTHROPIC_IMAGE_MAX_BYTES):
        self.sdk = sdk
        self.reasoning_effort = reasoning_effort
        self.trace = trace or (lambda message: None)
        self.image_max_bytes = image_max_bytes
        self._images = {}

    def create(self, **request):
        params = self.messages_request(request)
        try:
            message = self._send(params)
        except anthropic.BadRequestError as exc:
            # drop_block covers thinking blocks from an edited conversation; a
            # block whose own text was changed (for example by redaction) is
            # still rejected. Answer this turn without the earlier thinking.
            text = str(exc)
            if "signature" not in text or "thinking" not in text:
                raise
            self.trace(
                "[anthropic] An earlier thinking block was rejected; "
                "resending this request without earlier thinking."
            )
            params["messages"] = without_thinking(params["messages"])
            message = self._send(params)
        return self.response(message)

    def _send(self, params):
        with self.sdk.beta.messages.stream(**params) as stream:
            return stream.get_final_message()

    def messages_request(self, request):
        system, messages = self.messages(request.get("input") or [])
        params = {
            "model": request["model"],
            "max_tokens": int(request.get("max_output_tokens") or ANTHROPIC_MAX_TOKENS),
            "messages": messages,
            "thinking": copy.deepcopy(ANTHROPIC_THINKING),
            "betas": list(ANTHROPIC_BETAS),
        }
        if system:
            params["system"] = system
        output_config = {}
        effort = (request.get("reasoning") or {}).get("effort") or self.reasoning_effort
        if effort:
            output_config["effort"] = ANTHROPIC_EFFORTS.get(effort, effort)
        text_format = (request.get("text") or {}).get("format") or {}
        if text_format.get("type") == "json_schema":
            output_config["format"] = {
                "type": "json_schema",
                "schema": text_format["schema"],
            }
        if output_config:
            params["output_config"] = output_config
        tools = request.get("tools")
        if tools:
            params["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "input_schema": tool["parameters"],
                }
                for tool in tools
            ]
            if request.get("parallel_tool_calls") is False:
                params["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
            params["cache_control"] = dict(ANTHROPIC_CACHE_CONTROL)
        return params

    def messages(self, items):
        """Split Responses items into a system prompt and Messages API turns."""

        system_parts = []
        messages = []

        def add(role, blocks):
            if not blocks:
                return
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": list(blocks)})

        for item in items:
            item_type = item.get("type")
            role = item.get("role")
            if item_type == "reasoning":
                add("assistant", thinking_blocks(item))
            elif item_type == "function_call":
                add(
                    "assistant",
                    [
                        {
                            "type": "tool_use",
                            "id": item.get("call_id") or item.get("id"),
                            "name": item.get("name"),
                            "input": tool_input(item.get("arguments")),
                        }
                    ],
                )
            elif item_type == "function_call_output":
                add(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": item.get("call_id"),
                            "content": self.tool_result_content(item.get("output")),
                        }
                    ],
                )
            elif role in {"developer", "system"}:
                text = content_text(item.get("content"))
                if not messages:
                    system_parts.append(text)
                elif text.strip():
                    add("user", [{"type": "text", "text": text}])
            elif role == "assistant":
                add("assistant", self.content_blocks(item.get("content")))
            elif role == "user":
                blocks = self.content_blocks(item.get("content"))
                last = messages[-1]["content"][-1] if messages and messages[-1]["role"] == "user" else None
                if (
                    last is not None
                    and last.get("type") == "tool_result"
                    and any(block["type"] == "image" for block in blocks)
                ):
                    # The image view_image loaded is that tool call's result.
                    if isinstance(last["content"], str):
                        last["content"] = [{"type": "text", "text": last["content"]}]
                    last["content"].extend(blocks)
                else:
                    add("user", blocks)
        return "\n\n".join(part for part in system_parts if part), messages

    def tool_result_content(self, output):
        """A tool's output: text, or a list of text and images from the modeling agent's tools."""

        if isinstance(output, list):
            return self.content_blocks(output)
        return str(output or "")

    def content_blocks(self, content):
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content.strip() else []
        blocks = []
        for part in content or []:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"input_text", "output_text", "text"}:
                text = str(part.get("text") or "")
                if text.strip():
                    blocks.append({"type": "text", "text": text})
            elif part_type == "input_image":
                blocks.append(self.image_block(str(part.get("image_url") or "")))
        return blocks

    def image_block(self, url):
        if not (url.startswith("data:") and ";base64," in url):
            return {"type": "image", "source": {"type": "url", "url": url}}
        header, data = url.split(",", 1)
        media_type = header[len("data:"):].split(";", 1)[0]
        media_type, data = self.fit_image(media_type, data)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }

    def fit_image(self, media_type, data):
        """The image as Claude accepts it: at most 2000 px on a side, in a
        format it reads. The result is the same every time for the same image,
        so earlier turns are sent unchanged."""

        key = hashlib.sha256(f"{media_type},{data}".encode("ascii")).hexdigest()
        if key in self._images:
            return self._images[key]
        raw = base64.b64decode(data)
        with Image.open(io.BytesIO(raw)) as image:
            if (
                media_type in ANTHROPIC_IMAGE_TYPES
                and len(raw) <= self.image_max_bytes
                and max(image.size) <= ANTHROPIC_IMAGE_MAX_EDGE
            ):
                fitted = (media_type, data)
            else:
                image.thumbnail((ANTHROPIC_IMAGE_MAX_EDGE, ANTHROPIC_IMAGE_MAX_EDGE))
                if image.mode not in {"RGB", "RGBA", "L", "LA"}:
                    image = image.convert("RGBA")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                if buffer.tell() > self.image_max_bytes:
                    buffer = io.BytesIO()
                    image.convert("RGB").save(buffer, format="JPEG", quality=90)
                    fitted_type = "image/jpeg"
                else:
                    fitted_type = "image/png"
                fitted = (fitted_type, base64.b64encode(buffer.getvalue()).decode("ascii"))
        self._images[key] = fitted
        return fitted

    def response(self, message):
        for change in getattr(message, "input_transformations", None) or []:
            if getattr(change, "type", None) == "thinking_dropped":
                self.trace(
                    f"[anthropic] Dropped an earlier thinking block at {change.path} "
                    f"({change.reason})."
                )
        if message.stop_reason == "refusal":
            details = message.stop_details
            category = getattr(details, "category", None) or "unspecified"
            explanation = getattr(details, "explanation", None) or ""
            raise ModelRefusal(
                f"{message.model} declined this request (category: {category}). {explanation}".strip()
            )
        if message.stop_reason == "model_context_window_exceeded":
            raise RuntimeError(f"{message.model} ran out of context window space.")

        blocks = list(message.content)
        # After a fallback partway through a turn, the declined attempt's thinking
        # and tool calls are not replayed; its text is.
        boundary = max((index for index, block in enumerate(blocks) if block.type == "fallback"), default=-1)
        blocks = [block for index, block in enumerate(blocks) if index > boundary or block.type == "text"]
        incomplete = message.stop_reason == "max_tokens"
        if incomplete and blocks and blocks[-1].type == "tool_use":
            # Cut off while writing the call, so its arguments are incomplete.
            blocks.pop()
        output = []
        for block in blocks:
            if block.type == "thinking":
                summary = [{"type": "summary_text", "text": block.thinking}] if block.thinking else []
                output.append(
                    {"type": "reasoning", "summary": summary, "encrypted_content": block.signature}
                )
            elif block.type == "redacted_thinking":
                output.append(
                    {"type": "reasoning", "summary": [], "encrypted_content": block.data, "redacted": True}
                )
            elif block.type == "text":
                output.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": block.text, "annotations": []}],
                    }
                )
            elif block.type == "tool_use":
                output.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    }
                )
        usage = message.usage
        self.trace(
            f"[usage] {message.model} input={usage.input_tokens} "
            f"cache_read={usage.cache_read_input_tokens or 0} "
            f"cache_write={usage.cache_creation_input_tokens or 0} "
            f"output={usage.output_tokens} stop={message.stop_reason}"
        )
        return SimpleNamespace(
            id=message.id,
            model=message.model,
            output=output,
            output_text="".join(block.text for block in blocks if block.type == "text"),
            status="incomplete" if incomplete else "completed",
            incomplete_details={"reason": "max_output_tokens"} if incomplete else None,
            usage=usage,
        )


def thinking_blocks(item):
    data = item.get("encrypted_content")
    if not data:
        # A reasoning item without a signature cannot be replayed to Claude.
        return []
    if item.get("redacted"):
        return [{"type": "redacted_thinking", "data": data}]
    text = "".join(
        str(part.get("text") or "")
        for part in item.get("summary") or []
        if isinstance(part, dict)
    )
    return [{"type": "thinking", "thinking": text, "signature": data}]


def tool_input(arguments):
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def content_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text") or "")
        for part in content or []
        if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}
    )


def without_thinking(messages):
    stripped = []
    for message in messages:
        content = [
            block
            for block in message["content"]
            if block.get("type") not in {"thinking", "redacted_thinking"}
        ]
        if content:
            stripped.append({**message, "content": content})
    return stripped
