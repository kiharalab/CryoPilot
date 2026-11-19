"""Estimated cost of model requests, from the providers' published list prices.

Prices are US dollars per million tokens. Each response is priced when it
arrives and added to the session's total, so a later change here does not
rewrite what a finished session cost. A model not listed here has no estimate;
the session then shows tokens only and its spending limit cannot apply.

Sources, checked on PRICES_AS_OF:
    https://developers.openai.com/api/docs/pricing
    https://platform.claude.com/docs/en/about-claude/pricing
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PRICES_AS_OF = "2026-11-09"

# A provider's dated snapshot of a model, such as gpt-5.5-2026-04-23, has the model's price.
_SNAPSHOT = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")
# OpenAI prices a whole request higher above this much input. The rates for its
# flagship models double for input and rise by half for output.
OPENAI_LONG_CONTEXT = 272_000
# Claude Haiku 5.5 prices a request five times higher above this much input.
HAIKU_LONG_CONTEXT = 100_000


@dataclass(frozen=True)
class Price:
    input: float
    cached: float
    output: float
    write: float  # cache write, short lifetime
    write_long: float  # cache write, long lifetime
    long_context: int | None = None
    long_input_factor: float = 1.0
    long_output_factor: float = 1.0


def _openai(input: float, cached: float, output: float, write: float | None = None, write_long: float | None = None, long_context: bool = True) -> Price:
    return Price(
        input,
        cached,
        output,
        input if write is None else write,
        input if write_long is None else write_long,
        OPENAI_LONG_CONTEXT if long_context else None,
        2.0,
        1.5,
    )


def _claude(input: float, cached: float, output: float, **long_rates: Any) -> Price:
    # Claude's 5 minute cache costs 1.25 times the input price to write, and its 1 hour cache 2 times.
    return Price(input, cached, output, input * 1.25, input * 2.0, **long_rates)


PRICES: dict[str, Price] = {
    "gpt-6-astra": _openai(10.00, 1.00, 50.00, 12.50, 25.00),
    "gpt-6.1-sol": _openai(2.00, 0.10, 10.00, 2.50, 5.00),
    "gpt-6.1-terra": _openai(1.00, 0.05, 5.00, 1.25, 2.50),
    "gpt-6-sol": _openai(2.00, 0.20, 10.00, 2.50, 5.00),
    "gpt-6-luna": _openai(0.10, 0.01, 0.50, 0.125, 0.25),
    # OpenAI lists this price as promotional until 2026-11-30.
    "gpt-5.6-sol": _openai(4.00, 0.40, 20.00, 5.00, 10.00),
    "gpt-5.6-terra": _openai(2.00, 0.20, 12.00, 2.50, 5.00),
    "gpt-5.6-luna": _openai(0.20, 0.02, 1.20, 0.25, 0.50),
    "gpt-5.5": _openai(5.00, 0.50, 30.00),
    "gpt-5.5-pro": _openai(30.00, 30.00, 180.00),
    "gpt-5.4": _openai(2.50, 0.25, 15.00),
    "gpt-5.4-pro": _openai(30.00, 30.00, 180.00),
    "gpt-5.4-mini": _openai(0.75, 0.075, 4.50, long_context=False),
    "gpt-5.4-nano": _openai(0.20, 0.02, 1.25, long_context=False),
    "gpt-5.2": _openai(1.75, 0.175, 14.00, long_context=False),
    "gpt-5.2-pro": _openai(21.00, 21.00, 168.00, long_context=False),
    "gpt-5.1": _openai(1.25, 0.125, 10.00, long_context=False),
    "gpt-5": _openai(1.25, 0.125, 10.00, long_context=False),
    "gpt-5-mini": _openai(0.25, 0.025, 2.00, long_context=False),
    "gpt-5-nano": _openai(0.05, 0.005, 0.40, long_context=False),
    "gpt-5-pro": _openai(15.00, 15.00, 120.00, long_context=False),
    "claude-fable-5-1": _claude(10.00, 0.25, 50.00),
    "claude-fable-5": _claude(10.00, 1.00, 50.00),
    "claude-opus-5-5": _claude(4.00, 0.20, 20.00),
    "claude-opus-5": _claude(5.00, 0.50, 25.00),
    "claude-opus-4-8": _claude(5.00, 0.50, 25.00),
    "claude-opus-4-7": _claude(5.00, 0.50, 25.00),
    "claude-opus-4-6": _claude(5.00, 0.50, 25.00),
    "claude-sonnet-5-5": _claude(2.00, 0.10, 10.00),
    "claude-sonnet-5": _claude(2.00, 0.20, 10.00),
    "claude-sonnet-4-6": _claude(3.00, 0.30, 15.00),
    "claude-haiku-4-5": _claude(1.00, 0.10, 5.00),
    "claude-haiku-5-5": _claude(
        0.10, 0.01, 0.50, long_context=HAIKU_LONG_CONTEXT, long_input_factor=5.0, long_output_factor=5.0
    ),
}


def price_for(model: str | None) -> Price | None:
    name = str(model or "").strip().lower()
    return PRICES.get(name) or PRICES.get(_SNAPSHOT.sub("", name))


def is_free(model: str | None, provider: str | None = None) -> bool:
    """A model on this computer (Ollama) costs nothing per token."""

    return provider == "ollama"


def estimate(model: str | None, record: dict[str, Any], provider: str | None = None) -> float | None:
    """The cost of one response in dollars, or None when the model's price is not known."""

    if is_free(model, provider):
        return 0.0
    price = price_for(model)
    if price is None:
        return None
    uncached = int(record.get("input_tokens") or 0)
    cached = int(record.get("cached_tokens") or 0)
    written = int(record.get("cache_write_tokens") or 0)
    written_long = int(record.get("cache_write_long_tokens") or 0)
    output = int(record.get("output_tokens") or 0)
    long_request = price.long_context is not None and uncached + cached + written + written_long > price.long_context
    input_factor = price.long_input_factor if long_request else 1.0
    output_factor = price.long_output_factor if long_request else 1.0
    total = (
        uncached * price.input * input_factor
        + cached * price.cached * input_factor
        + written * price.write * input_factor
        + written_long * price.write_long * input_factor
        + output * price.output * output_factor
    )
    return total / 1_000_000
