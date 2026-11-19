import argparse
import base64
import contextlib
import contextvars
import copy
import io
import json
import mimetypes
import os
import re
import shlex
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import llm_client
import web_tools
from cryosparc_repl import (
    CRYOSPARC_HELP,
    connect_cryosparc,
    connect_cryosparc_from_env,
    job_summary,
)
from llm_client import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    is_context_window_error,
    response_output_items,
    response_text,
)
from session_store import RecursiveLimitError, SessionNotFoundError, SessionStore
from startup_cli import (
    WORKSPACE_TITLE,
    EXISTING_PROJECT,
    bind_manifest_scope,
    build_developer_instruction,
    build_user_prompt,
    collect_cryosparc_credentials,
    collect_continuation_manifest,
    collect_session_manifest,
    prompt_required,
)


MODEL = llm_client.DEFAULT_MODELS["openai"]
REASONING_EFFORT = os.environ.get(
    "OPENAI_REASONING_EFFORT",
    os.environ.get("REASONING_EFFORT", "xhigh"),
).strip()
WAIT_INTERVAL = float(os.environ.get("WAIT_INTERVAL", "5"))
TRACE_TOOLS = os.environ.get("TRACE_TOOLS", "1").lower() not in {"0", "false", "no"}
CONTEXT_COMPACTION_ENABLED = os.environ.get("CONTEXT_COMPACTION", "1").lower() not in {
    "0",
    "false",
    "no",
}
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "1000000"))
CONTEXT_COMPACTION_THRESHOLD = float(os.environ.get("CONTEXT_COMPACTION_THRESHOLD", "0.23"))
CONTEXT_COMPACTION_TARGET_RATIO = float(os.environ.get("CONTEXT_COMPACTION_TARGET_RATIO", "0.20"))
COMPACT_MODEL = os.environ.get("COMPACT_MODEL")
# Reasoning models count hidden reasoning against max_output_tokens. Summary
# calls therefore use their own (low) reasoning effort and a budget large
# enough for the JSON checkpoint, and retry with more room if cut off.
COMPACT_MAX_TOKENS = int(os.environ.get("COMPACT_MAX_TOKENS", "16000"))
COMPACT_MAX_TOKENS_CEILING = 64000
COMPACT_REASONING_EFFORT = os.environ.get("COMPACT_REASONING_EFFORT", "low").strip()
# Largest stdout/stderr/traceback text a run_code call returns into context.
TOOL_OUTPUT_MAX_CHARS = int(os.environ.get("TOOL_OUTPUT_MAX_CHARS", "60000"))
RLM_MAX_DEPTH = int(os.environ.get("RLM_MAX_DEPTH", "4"))
RLM_MAX_CALLS = int(os.environ.get("RLM_MAX_CALLS", "16"))
RLM_MAX_MODEL_TURNS = int(os.environ.get("RLM_MAX_MODEL_TURNS", "64"))


client = None
agent_memory = {
    "full_history": [],
    "active_messages": [],
    "compactions": [],
    "last_summary": None,
}
session_runtime = {
    "store": None,
    "session_id": None,
    "active_item_ids": [],
    "last_summary_item_id": None,
    "durable_state": {},
    "sensitive_values": [],
    "trusted_instructions": None,
    "root_turn_item_id": None,
    "bound_csw": None,
    "bound_scope": None,
    "recursion_config": {
        "max_depth": RLM_MAX_DEPTH,
        "max_calls": RLM_MAX_CALLS,
        "max_model_turns": RLM_MAX_MODEL_TURNS,
    },
}
# Processing guides the agent can load, in guides/. Select one with
# CRYOEM_GUIDE (or --processing-guide); a path to another Markdown guide
# also works.
GUIDES_DIR = Path(__file__).with_name("guides")
CRYOEM_GUIDES = {
    "original": GUIDES_DIR / "cryoem-data-processing.md",
    "experimental": GUIDES_DIR / "self-evolution-summary.md",
}
DEFAULT_CRYOEM_GUIDE = "original"


def resolve_cryoem_guide(choice=None):
    choice = str(choice or os.environ.get("CRYOEM_GUIDE") or DEFAULT_CRYOEM_GUIDE).strip()
    if choice.lower() in CRYOEM_GUIDES:
        return CRYOEM_GUIDES[choice.lower()]
    if choice.lower().endswith(".md"):
        return Path(choice).expanduser()
    raise ValueError(
        f"Unknown processing guide {choice!r}; use one of "
        f"{sorted(CRYOEM_GUIDES)} or a path to a .md file."
    )


CRYOEM_GUIDE_PATH = resolve_cryoem_guide()

SECRET_FIELD_NAMES = {
    "api_key",
    "access_key",
    "private_key",
    "secret_key",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "authorization",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "credential",
    "credentials",
}
MIN_SECRET_SUBSTRING_LENGTH = 12
SECRET_LABEL_RE = re.compile(
    r"""
    (?P<prefix>
        (?P<key_quote>[\"'])?
        (?:
            api[\s_-]*key
            |access[\s_-]*(?:key|token)
            |private[\s_-]*key
            |secret[\s_-]*key
            |refresh[\s_-]*token
            |id[\s_-]*token
            |auth(?:entication|orization)?[\s_-]*token
            |client[\s_-]*secret
            |password
            |passwd
            |secret
            |credentials?
            |token
            |authorization
        )
        (?(key_quote)(?P=key_quote))
        \s*[:=]\s*
    )
    (?P<value>
        \"(?:\\.|[^\"\\])*\"
        |'(?:\\.|[^'\\])*'
        |[^\s,;}\]]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class RLMError(RuntimeError):
    """Base class for recursive-agent failures exposed inside the REPL."""


class RLMInputError(RLMError, ValueError):
    """Raised when a recursive call receives unsafe or malformed messages."""


class RLMUsageError(RLMError):
    """Raised when recursion is attempted outside an active agent tool frame."""


class RLMBudgetExceeded(RLMError):
    """Raised before recursive work that would exceed a saved session limit."""


class RLMScopeViolation(RLMError):
    """Raised when recursive code leaves the bound CryoSPARC scope."""


class RLMProviderError(RLMError):
    """Raised when a recursive provider request cannot be completed safely."""


@dataclass
class AgentCallFrame:
    kind: Literal["root", "child"]
    messages: list[dict]
    trusted_instructions: str
    depth: int = 0
    frame_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    recursive_call_id: int | None = None
    parent_recursive_call_id: int | None = None
    root_turn_item_id: int | None = None
    root_function_call_item_id: int | None = None
    root_function_call_id: str | None = None
    active_item_ids: list[int] = field(default_factory=list)
    last_summary: dict | None = None
    model_turns: int = 0
    tool_calls: int = 0


@dataclass(frozen=True)
class ToolInvocationContext:
    frame: AgentCallFrame
    item_id: int | None
    call_id: str
    name: str


current_agent_frame = contextvars.ContextVar("current_agent_frame", default=None)
current_tool_invocation = contextvars.ContextVar("current_tool_invocation", default=None)
evaluating_wait_condition = contextvars.ContextVar("evaluating_wait_condition", default=False)


# Set by the web app's driver. The first runs before each model request and can
# hold it while the run is paused; the second receives each response's tokens.
before_model_request = None
on_model_usage = None


def create_response(**kwargs):
    if "input" in kwargs:
        kwargs["input"] = redact_persisted_value(kwargs["input"])
    if before_model_request is not None:
        before_model_request()
    response = client.create(**kwargs)
    if on_model_usage is not None:
        record = llm_client.usage_record(
            getattr(response, "usage", None), getattr(response, "model", None) or kwargs.get("model")
        )
        if record is not None:
            on_model_usage(record)
    return response


def slugify_heading(title):
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return slug or "section"


def load_cryoem_data_processing(path=CRYOEM_GUIDE_PATH):
    path = Path(path)
    guide = {
        "path": str(path),
        "exists": path.exists(),
        "load_error": None,
        "raw_text": "",
        "outline": [],
        "sections": [],
        "by_slug": {},
        "by_title": {},
    }
    if not path.exists():
        guide["load_error"] = f"Guide file not found: {path}"
        return guide

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        guide["load_error"] = f"Could not read guide file: {exc}"
        return guide

    guide["raw_text"] = text
    lines = text.splitlines()
    current = None
    slug_counts = {}

    def finalize_section(line_end):
        if current is None:
            return
        content = "\n".join(current.pop("_content_lines")).strip()
        current["content"] = content
        current["line_end"] = line_end
        heading = "#" * current["level"] + " " + current["title"]
        current["text"] = f"{heading}\n\n{content}".strip()
        guide["sections"].append(current)
        guide["outline"].append(
            {
                "level": current["level"],
                "title": current["title"],
                "slug": current["slug"],
                "line_start": current["line_start"],
                "line_end": current["line_end"],
            }
        )

    for line_number, line in enumerate(lines, start=1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            if current is not None:
                current["_content_lines"].append(line)
            continue

        finalize_section(line_number - 1)

        title = match.group(2)
        base_slug = slugify_heading(title)
        slug_counts[base_slug] = slug_counts.get(base_slug, 0) + 1
        slug = base_slug if slug_counts[base_slug] == 1 else f"{base_slug}-{slug_counts[base_slug]}"
        current = {
            "level": len(match.group(1)),
            "title": title,
            "slug": slug,
            "line_start": line_number,
            "_content_lines": [],
        }

    finalize_section(len(lines))

    guide["by_slug"] = {section["slug"]: section for section in guide["sections"]}
    by_title = {}
    for section in guide["sections"]:
        by_title.setdefault(section["title"].lower(), []).append(section["slug"])
    guide["by_title"] = by_title
    return guide


cryoem_data_processing = load_cryoem_data_processing()
cryoem_guide_sections = cryoem_data_processing["sections"]


def _cryoem_guide_excerpt(text, query, max_chars=420):
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    query = str(query or "").strip().lower()
    idx = text.lower().find(query) if query else -1
    if idx < 0:
        return text[: max_chars - 3] + "..."
    start = max(0, idx - max_chars // 3)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end] + suffix


def cryoem_guide_search(term, limit=8):
    """Search the persisted cryo-EM processing guide sections."""
    query = str(term or "").strip().lower()
    limit = int(limit)
    if not query:
        return copy.deepcopy(cryoem_data_processing["outline"][:limit])

    terms = [part for part in re.split(r"\s+", query) if part]
    rows = []
    for section in cryoem_data_processing["sections"]:
        title = section["title"].lower()
        content = section["content"].lower()
        score = 0
        for part in terms:
            score += title.count(part) * 10
            score += content.count(part)
        if score:
            rows.append(
                {
                    "slug": section["slug"],
                    "title": section["title"],
                    "level": section["level"],
                    "line_start": section["line_start"],
                    "line_end": section["line_end"],
                    "score": score,
                    "excerpt": _cryoem_guide_excerpt(section["content"], query),
                }
            )
    rows.sort(key=lambda row: (-row["score"], row["line_start"]))
    return rows[:limit]


def cryoem_guide_section(key):
    """Return one full guide section by slug, exact title, or title substring."""
    key = str(key or "").strip()
    if not key:
        return None
    lowered = key.lower()
    if key in cryoem_data_processing["by_slug"]:
        return copy.deepcopy(cryoem_data_processing["by_slug"][key])
    if lowered in cryoem_data_processing["by_slug"]:
        return copy.deepcopy(cryoem_data_processing["by_slug"][lowered])
    slugs = cryoem_data_processing["by_title"].get(lowered)
    if slugs:
        return copy.deepcopy(cryoem_data_processing["by_slug"][slugs[0]])
    for section in cryoem_data_processing["sections"]:
        if lowered in section["title"].lower() or lowered == section["slug"].lower():
            return copy.deepcopy(section)
    return None


def reload_cryoem_data_processing(path=None):
    """Reload the selected processing guide, e.g. after editing it."""
    global cryoem_data_processing, cryoem_guide_sections
    cryoem_data_processing = load_cryoem_data_processing(path or CRYOEM_GUIDE_PATH)
    cryoem_guide_sections = cryoem_data_processing["sections"]
    python_globals.update(
        {
            "cryoem_data_processing": cryoem_data_processing,
            "cryoem_processing_guide": cryoem_data_processing,
            "cryoem_guide_sections": cryoem_guide_sections,
        }
    )
    return {
        "path": cryoem_data_processing["path"],
        "exists": cryoem_data_processing["exists"],
        "load_error": cryoem_data_processing["load_error"],
        "sections": len(cryoem_data_processing["sections"]),
    }


python_globals = {
    "__name__": "__agent_repl__",
    "agent_memory": agent_memory,
    "conversation_history": agent_memory["full_history"],
    "cryoem_data_processing": cryoem_data_processing,
    "cryoem_processing_guide": cryoem_data_processing,
    "cryoem_guide_sections": cryoem_guide_sections,
    "cryoem_guide_search": cryoem_guide_search,
    "cryoem_guide_section": cryoem_guide_section,
    "reload_cryoem_data_processing": reload_cryoem_data_processing,
    "connect_cryosparc": connect_cryosparc,
    "connect_cryosparc_from_env": connect_cryosparc_from_env,
    "job_summary": job_summary,
    "session_state": session_runtime["durable_state"],
}


TOOLS = [
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
                "poll_seconds": {"type": "number", "default": WAIT_INTERVAL},
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

# Experimental: web search and page reading, when the app turns them on for this session.
WEB_TOOLS_ENABLED = web_tools.enabled(os.environ)
if WEB_TOOLS_ENABLED:
    TOOLS = [*TOOLS, *web_tools.TOOL_SCHEMAS]


RLM_INSTRUCTIONS = """
Recursive delegation is available inside the persistent Python environment as
`rlm(messages)` and `rlm.run(messages)`. Each call synchronously runs a child
agent with the same authenticated `csw`, Python globals, run_code, run_and_wait,
and view_image capabilities, and it inherits every developer instruction and
the exact session/project/workspace scope. Delegate one bounded subproblem at a
time. Because this REPL uses normal Python `exec`, use the returned string in
code or print it, for example `answer = rlm([...]); print(answer)`.
Use `rlm.calls()` to list durable recursive branches for this session and
`rlm.history(call_id, include_descendants=False)` to inspect one exact redacted
branch trace after an interruption or when detailed provenance is needed.

Recursive children are appropriate for bounded 2D-classification iterations
and similar iterative scientific work. A child may create, run, wait for, and
inspect one 2D-classification job, then report the evidence needed to choose the
next class selection, parameter change, or iteration.

Recursive delegation never relaxes the one/few-jobs rule, never authorizes a
static multi-job workflow, and must not reconnect to CryoSPARC or switch the
bound project/workspace. The current frame's conversation is available as a
detached `messages` snapshot; changing it does not change durable history.
""".strip()


RECURSIVE_CHILD_OVERLAY = """
You are a recursive child of the active cryo-EM agent. Solve only the supplied
subproblem and return a concise result to the parent. You share the parent's
authenticated Python environment and CryoSPARC scope, and every inherited
scientific, safety, waiting, and one/few-jobs instruction remains mandatory.
You may recursively delegate another bounded subproblem through `rlm`, subject
to the shared session limits. Do not ask for credentials, reconnect, create a
project/workspace, or switch scope. In a child frame, `agent_memory` is the root
transcript; inspect this branch's exact durable trace with
`rlm.history(rlm_call_id)` when needed.
""".strip()


INSTRUCTIONS = f"""
You are an agent that can use CryoSPARC to analyze cryo-EM data. You can run Python code in a persistent REPL to inspect and manipulate data, and you can wait for conditions to be met before executing code. Use the provided tools to interact with CryoSPARC and perform tasks as needed.

During processing you MUST work on one or few jobs at a time. You must not run code to create multiple jobs in a static workflow. 
Rely on run_and_wait to wait for jobs to complete before proceeding to the next step. You adapt to the data and focus on getting the highest resolution structure possible. You can use the view_image tool to load images into the conversation for visual inspection.
You don't have to instantiate csw again, it is already available.

Long conversations may be compacted to keep active model context small. When that happens, the full exact transcript remains available in the REPL as `agent_memory["full_history"]`, also aliased as `conversation_history`. Use `memory_search(term, limit=20)` and `memory_slice(start, end)` from `run_code` to inspect older exact messages. Do not dump the whole history unless the user explicitly asks.
`run_code` output longer than {TOOL_OUTPUT_MAX_CHARS} characters is clipped in the middle; the full text of the latest call stays in `last_tool_output`. Print focused summaries rather than whole datasets or histories.

A structured cryo-EM data-processing guide is also persisted in the REPL. Use it when choosing workflows, parameters, diagnostics, or corrective actions:
- `cryoem_data_processing`: dict loaded from the selected guide file (see its `path`) with `raw_text`, `outline`, `sections`, `by_slug`, `by_title`, `path`, and load status.
- `cryoem_guide_search(term, limit=8)`: search guide sections and return titles, slugs, line ranges, scores, and excerpts.
- `cryoem_guide_section(key)`: return a full section by slug, exact title, or title substring.
- `reload_cryoem_data_processing()`: reload the guide after the Markdown file is edited.
Prefer `cryoem_guide_search(...)` and `cryoem_guide_section(...)` over dumping `raw_text` unless the user explicitly asks for the whole guide.
Before the first job of each stage (for example exposure curation, particle picking, 2D classification, ab initio reconstruction, refinement, heterogeneity analysis, or CTF and motion refinement), read the guide section for that stage.

You have access to the following tools within the REPL:
{CRYOSPARC_HELP}

{RLM_INSTRUCTIONS}
"""


COMPACTION_SUMMARY_PREFIX = "[CONTEXT COMPACTION - REFERENCE ONLY]"
COMPACTION_SCHEMA_VERSION = "cryopilot_compaction_v1"
SUMMARY_INPUT_MAX_CHARS = 240000
SUMMARY_MESSAGE_MAX_CHARS = 6000
TOOL_ARGS_SUMMARY_MAX_CHARS = 1500
IMAGE_TOKEN_ESTIMATE = 1600
CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 6
MIN_TAIL_MESSAGES = 4


class CompactionSummaryGenerationError(ValueError):
    """Raised after both summary attempts fail, retaining the last raw output."""

    def __init__(self, message, *, raw_output=None):
        super().__init__(message)
        self.raw_output = raw_output


COMPACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string", "const": COMPACTION_SCHEMA_VERSION},
        "current_goal": {"type": "string"},
        "current_stage": {"type": "string"},
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "job_uid": {"type": "string"},
                    "job_type": {"type": "string"},
                    "status": {"type": "string"},
                    "purpose": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "key_parameters": {"type": "array", "items": {"type": "string"}},
                    "results": {"type": "array", "items": {"type": "string"}},
                    "next_action": {"type": "string"},
                },
                "required": [
                    "job_uid",
                    "job_type",
                    "status",
                    "purpose",
                    "inputs",
                    "key_parameters",
                    "results",
                    "next_action",
                ],
                "additionalProperties": False,
            },
        },
        "artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "description": {"type": "string"},
                    "source_job_uid": {"type": ["string", "null"]},
                },
                "required": ["path", "description", "source_job_uid"],
                "additionalProperties": False,
            },
        },
        "selections": {"type": "array", "items": {"type": "string"}},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string"},
                    "rationale": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["decision", "rationale", "evidence"],
                "additionalProperties": False,
            },
        },
        "observations": {"type": "array", "items": {"type": "string"}},
        "failures_and_warnings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "recovery": {"type": "string"},
                },
                "required": ["description", "status", "recovery"],
                "additionalProperties": False,
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "next_actions": {"type": "array", "items": {"type": "string"}},
        "resume_notes": {"type": "array", "items": {"type": "string"}},
        "memory_search_terms": {"type": "array", "items": {"type": "string"}},
        "summary_failure": {"type": ["string", "null"]},
    },
    "required": [
        "schema_version",
        "current_goal",
        "current_stage",
        "jobs",
        "artifacts",
        "selections",
        "decisions",
        "observations",
        "failures_and_warnings",
        "open_questions",
        "next_actions",
        "resume_notes",
        "memory_search_terms",
        "summary_failure",
    ],
    "additionalProperties": False,
}


def reset_agent_memory():
    agent_memory["full_history"].clear()
    agent_memory["active_messages"].clear()
    agent_memory["compactions"].clear()
    agent_memory["last_summary"] = None
    session_runtime["active_item_ids"] = []
    session_runtime["last_summary_item_id"] = None
    session_runtime["durable_state"] = {}
    if "python_globals" in globals():
        python_globals["session_state"] = session_runtime["durable_state"]
        python_globals["conversation_history"] = agent_memory["full_history"]


def remember_messages(messages):
    agent_memory["full_history"].extend(copy.deepcopy(messages))


def set_active_messages(messages):
    agent_memory["active_messages"].clear()
    agent_memory["active_messages"].extend(copy.deepcopy(messages))


def configure_session_runtime(store=None, session_id=None):
    session_runtime["store"] = store
    session_runtime["session_id"] = session_id
    session_runtime["active_item_ids"] = []
    session_runtime["last_summary_item_id"] = None
    session_runtime["durable_state"] = {}
    session_runtime["sensitive_values"] = []
    session_runtime["trusted_instructions"] = None
    session_runtime["root_turn_item_id"] = None
    session_runtime["bound_csw"] = None
    session_runtime["bound_scope"] = None
    session_runtime["recursion_config"] = {
        "max_depth": RLM_MAX_DEPTH,
        "max_calls": RLM_MAX_CALLS,
        "max_model_turns": RLM_MAX_MODEL_TURNS,
    }
    python_globals.pop("csw", None)
    python_globals.pop("session_context", None)
    python_globals.pop("messages", None)
    python_globals.pop("rlm_depth", None)
    python_globals.pop("rlm_call_id", None)
    python_globals["session_state"] = session_runtime["durable_state"]
    python_globals["conversation_history"] = agent_memory["full_history"]
    if store is not None and session_id:
        register_environment_secrets()


def register_sensitive_value(value):
    value = str(value or "")
    if value and value not in session_runtime["sensitive_values"]:
        session_runtime["sensitive_values"].append(value)


def is_secret_field_name(name):
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    if normalized in SECRET_FIELD_NAMES:
        return True
    parts = set(normalized.split("_"))
    if parts.intersection(
        {"password", "passwd", "secret", "credential", "credentials", "token"}
    ):
        return True
    return normalized.endswith(
        (
            "_api_key",
            "_access_key",
            "_private_key",
            "_secret_key",
            "_access_token",
            "_refresh_token",
            "_id_token",
            "_auth_token",
            "_client_secret",
        )
    )


def register_environment_secrets(environ=None):
    environ = os.environ if environ is None else environ
    for name, value in environ.items():
        if is_secret_field_name(name):
            register_sensitive_value(value)


def redact_runtime_secrets(value):
    text = str(value or "")
    for secret in sorted(
        session_runtime.get("sensitive_values") or [], key=len, reverse=True
    ):
        if len(secret) >= MIN_SECRET_SUBSTRING_LENGTH:
            text = text.replace(secret, "[REDACTED]")
        elif text.strip() == secret:
            # Avoid turning every occurrence of a one-character/common password
            # into a protocol-breaking replacement. A directly printed short
            # credential is still removed, and labelled forms are handled by
            # redact_summary_text below.
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 [REDACTED]",
        text,
    )
    return redact_summary_text(text)


def redact_persisted_value(value, field_name=None):
    """Return a JSON-compatible copy with credential-bearing values removed."""

    if field_name == "encrypted_content":
        # This is authenticated, opaque protocol data returned by OpenAI.
        # Treating random ciphertext as human-readable text can produce false
        # secret matches (for example an incidental ``sk-`` substring) and
        # corrupt the value so the Responses API can no longer verify it.
        return copy.deepcopy(value)
    if (
        field_name in {"image_url", "url"}
        and isinstance(value, str)
        and value.startswith("data:image/")
        and ";base64," in value
    ):
        # Harness-loaded image bytes are opaque protocol payloads. Searching
        # their base64 representation for plaintext secret substrings can
        # produce false positives and silently corrupt the image.
        return value
    if field_name is not None and is_secret_field_name(field_name):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_runtime_secrets(value)
    if isinstance(value, dict):
        return {
            key: redact_persisted_value(item, field_name=key)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_persisted_value(item) for item in value]
    return copy.deepcopy(value)


def active_session_store():
    store = session_runtime.get("store")
    session_id = session_runtime.get("session_id")
    if store is None or not session_id:
        return None, None
    return store, session_id


def snapshot_workspace_jobs():
    state = copy.deepcopy(session_runtime.get("durable_state") or {})
    csw = python_globals.get("csw")
    if csw is None:
        return redact_persisted_value(state)
    try:
        state["known_jobs"] = redact_persisted_value(csw.list_jobs(limit=10000))
        state.pop("job_snapshot_error", None)
    except Exception as exc:
        state["job_snapshot_error"] = redact_runtime_secrets(
            truncate_middle(str(exc), 800)
        )
    state["workspace_snapshot_timestamp"] = time.time()
    state = redact_persisted_value(state)
    session_runtime["durable_state"] = copy.deepcopy(state)
    python_globals["session_state"] = session_runtime["durable_state"]
    return state


def current_durable_state(refresh_jobs=False):
    if refresh_jobs:
        return snapshot_workspace_jobs()
    return redact_persisted_value(session_runtime.get("durable_state") or {})


def persist_checkpoint(refresh_jobs=False):
    store, session_id = active_session_store()
    if store is None:
        return None
    active_ids = list(session_runtime["active_item_ids"])
    if len(active_ids) != len(agent_memory["active_messages"]):
        raise RuntimeError("Persistent active item IDs are not aligned with active messages.")
    return store.save_checkpoint(
        session_id,
        active_ids,
        last_summary_item_id=session_runtime.get("last_summary_item_id"),
        durable_state=current_durable_state(refresh_jobs=refresh_jobs),
    )


def persist_appended_items(items, active_messages, origin, refresh_jobs=False):
    items = redact_persisted_value(list(items))
    active_messages = redact_persisted_value(list(active_messages))
    store, session_id = active_session_store()
    new_ids = []
    if store is not None:
        new_ids, _checkpoint_id = store.append_items_and_checkpoint(
            session_id,
            origin,
            items,
            active_item_ids=session_runtime["active_item_ids"],
            last_summary_item_id=session_runtime.get("last_summary_item_id"),
            durable_state=current_durable_state(refresh_jobs=refresh_jobs),
        )
        session_runtime["active_item_ids"].extend(new_ids)
    remember_messages(items)
    set_active_messages(active_messages)
    return new_ids


def compaction_summary_from_message(message):
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.startswith(COMPACTION_SUMMARY_PREFIX):
        return None
    start = content.find("{")
    if start < 0:
        return None
    try:
        return validate_compaction_summary(json.loads(content[start:]))
    except (ValueError, json.JSONDecodeError):
        return None


def restore_session_memory(snapshot):
    reset_agent_memory()
    agent_memory["full_history"].extend(copy.deepcopy(snapshot.full_history))
    agent_memory["active_messages"].extend(copy.deepcopy(snapshot.active_messages))
    for record in snapshot.compactions:
        entry = copy.deepcopy(record.metrics)
        entry.update({"reason": record.reason, "ordinal": record.ordinal})
        agent_memory["compactions"].append(entry)
    if snapshot.last_summary is not None:
        agent_memory["last_summary"] = compaction_summary_from_message(snapshot.last_summary)
    session_runtime["active_item_ids"] = (
        list(snapshot.checkpoint.active_item_ids) if snapshot.checkpoint is not None else []
    )
    session_runtime["last_summary_item_id"] = (
        snapshot.checkpoint.last_summary_item_id if snapshot.checkpoint is not None else None
    )
    session_runtime["durable_state"] = (
        copy.deepcopy(snapshot.checkpoint.durable_state) if snapshot.checkpoint is not None else {}
    )
    python_globals["session_state"] = session_runtime["durable_state"]
    python_globals["conversation_history"] = agent_memory["full_history"]


def memory_slice(start, end=None):
    """Return exact message objects from the full in-process conversation history."""
    if end is None:
        end = start + 1
    return copy.deepcopy(agent_memory["full_history"][start:end])


def memory_search(term, limit=20):
    """Search compact previews of the exact conversation history."""
    term = str(term or "").lower()
    rows = []
    for idx, message in enumerate(agent_memory["full_history"]):
        preview = message_preview(message, max_chars=500)
        if not term or term in preview.lower():
            rows.append(
                {
                    "index": idx,
                    "role": message.get("role"),
                    "preview": preview,
                }
            )
        if len(rows) >= int(limit):
            break
    return rows


python_globals.update(
    {
        "memory_search": memory_search,
        "memory_slice": memory_slice,
    }
)


def estimate_text_tokens(text):
    return max(1, len(str(text or "")) // CHARS_PER_TOKEN)


def estimate_content_tokens(content):
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += estimate_text_tokens(part)
            elif isinstance(part, dict):
                part_type = part.get("type")
                if part_type in {"image_url", "input_image", "image"}:
                    total += IMAGE_TOKEN_ESTIMATE
                elif "text" in part:
                    total += estimate_text_tokens(part.get("text"))
                else:
                    total += estimate_text_tokens(json.dumps(part, ensure_ascii=False, default=str))
            else:
                total += estimate_text_tokens(part)
        return total
    return estimate_text_tokens(json.dumps(content, ensure_ascii=False, default=str))


def estimate_message_tokens(message):
    total = MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(
        f"{message.get('role', '')} {message.get('type', '')}"
    )
    total += estimate_content_tokens(message.get("content"))
    for key in ("name", "arguments", "call_id", "output"):
        if message.get(key):
            total += estimate_text_tokens(message.get(key))
    for tool_call in message.get("tool_calls") or []:
        total += MESSAGE_OVERHEAD_TOKENS
        if isinstance(tool_call, dict):
            total += estimate_text_tokens(json.dumps(tool_call, ensure_ascii=False, default=str))
        else:
            total += estimate_text_tokens(str(tool_call))
    if message.get("tool_call_id"):
        total += estimate_text_tokens(message.get("tool_call_id"))
    return total


def estimate_messages_tokens(messages):
    return sum(estimate_message_tokens(message) for message in messages)


def compactable_threshold_tokens():
    return int(CONTEXT_WINDOW * CONTEXT_COMPACTION_THRESHOLD)


def tail_budget_tokens():
    return max(1000, int(compactable_threshold_tokens() * CONTEXT_COMPACTION_TARGET_RATIO))


def truncate_middle(text, max_chars):
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head - 40
    return text[:head] + "\n...[truncated for compaction]...\n" + text[-tail:]


def redact_summary_text(text):
    def replace_labelled_secret(match):
        value = match.group("value")
        if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
            replacement = f"{value[0]}[REDACTED]{value[-1]}"
        else:
            replacement = "[REDACTED]"
        return f"{match.group('prefix')}{replacement}"

    text = SECRET_LABEL_RE.sub(replace_labelled_secret, str(text or ""))
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED_OPENAI_KEY]", text)
    return text


def content_to_summary_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return truncate_middle(content, SUMMARY_MESSAGE_MAX_CHARS)
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, str):
                pieces.append(truncate_middle(part, SUMMARY_MESSAGE_MAX_CHARS))
                continue
            if not isinstance(part, dict):
                pieces.append(truncate_middle(str(part), SUMMARY_MESSAGE_MAX_CHARS))
                continue
            part_type = part.get("type")
            if part_type in {"text", "input_text", "output_text"}:
                pieces.append(truncate_middle(part.get("text", ""), SUMMARY_MESSAGE_MAX_CHARS))
            elif part_type in {"image_url", "input_image", "image"}:
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    url = image_url.get("url", "")
                else:
                    url = str(image_url or part.get("url") or "")
                mime = url.split(";", 1)[0].replace("data:", "") if url.startswith("data:") else "image"
                pieces.append(f"[image omitted from summary: {mime}, original preserved in agent_memory]")
            else:
                pieces.append(
                    truncate_middle(
                        json.dumps(part, ensure_ascii=False, default=str),
                        SUMMARY_MESSAGE_MAX_CHARS,
                    )
                )
        return "\n".join(piece for piece in pieces if piece)
    return truncate_middle(json.dumps(content, ensure_ascii=False, default=str), SUMMARY_MESSAGE_MAX_CHARS)


def summarize_tool_calls(tool_calls):
    rows = []
    for tool_call in tool_calls or []:
        if not isinstance(tool_call, dict):
            rows.append(truncate_middle(str(tool_call), TOOL_ARGS_SUMMARY_MAX_CHARS))
            continue
        function = tool_call.get("function") or {}
        name = function.get("name") or "unknown"
        args = truncate_middle(function.get("arguments") or "{}", TOOL_ARGS_SUMMARY_MAX_CHARS)
        rows.append(f"tool_call id={tool_call.get('id')} name={name} args={args}")
    return "\n".join(rows)


def serialize_message_for_summary(message, index=None):
    label = f"[{index}] " if index is not None else ""
    role = message.get("role") or message.get("type") or "unknown"
    lines = [f"{label}{role}:"]
    content = content_to_summary_text(message.get("content"))
    if content:
        lines.append(content)
    if message.get("type") == "function_call":
        args = truncate_middle(message.get("arguments") or "{}", TOOL_ARGS_SUMMARY_MAX_CHARS)
        lines.append(
            f"function_call call_id={message.get('call_id')} "
            f"name={message.get('name')} args={args}"
        )
    if message.get("type") == "function_call_output":
        output = truncate_middle(message.get("output") or "", TOOL_ARGS_SUMMARY_MAX_CHARS)
        lines.append(f"function_call_output call_id={message.get('call_id')} output={output}")
    tool_calls = summarize_tool_calls(message.get("tool_calls"))
    if tool_calls:
        lines.append(tool_calls)
    if message.get("tool_call_id"):
        lines.append(f"tool_call_id={message.get('tool_call_id')}")
    return redact_summary_text("\n".join(lines).strip())


def serialize_messages_for_summary(messages):
    serialized = []
    for index, message in enumerate(messages):
        if is_compaction_summary_message(message):
            continue
        serialized.append(serialize_message_for_summary(message, index=index))
    text = "\n\n".join(serialized)
    if len(text) > SUMMARY_INPUT_MAX_CHARS:
        # Keep the opening turns (setup, inputs, first decisions) as well as
        # the most recent work.
        head = SUMMARY_INPUT_MAX_CHARS // 4
        tail = SUMMARY_INPUT_MAX_CHARS - head
        text = (
            text[:head]
            + "\n\n[Middle of the serialized compaction input was truncated]\n\n"
            + text[-tail:]
        )
    return text


def message_preview(message, max_chars=300):
    text = serialize_message_for_summary(message)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def is_compaction_summary_message(message):
    content = message.get("content")
    if isinstance(content, str):
        return content.startswith(COMPACTION_SUMMARY_PREFIX)
    return False


def protected_head_end(messages):
    if len(messages) <= 2:
        return len(messages)
    return 2


def tool_call_ids(message):
    ids = []
    if message.get("type") == "function_call" and message.get("call_id"):
        ids.append(message["call_id"])
    for tool_call in message.get("tool_calls") or []:
        if isinstance(tool_call, dict) and tool_call.get("id"):
            ids.append(tool_call["id"])
    return ids


def find_parent_tool_call(messages, tool_call_id, lower_bound):
    for idx in range(len(messages) - 1, lower_bound - 1, -1):
        if tool_call_id in tool_call_ids(messages[idx]):
            return idx
    return None


def include_preceding_response_context(messages, tail_start, head_end):
    while tail_start > head_end:
        previous = messages[tail_start - 1]
        if previous.get("type") not in {"reasoning", "function_call"}:
            break
        tail_start -= 1
    return tail_start


def align_tail_start_for_tool_groups(messages, tail_start, head_end):
    while tail_start < len(messages):
        tail_start = include_preceding_response_context(messages, tail_start, head_end)
        current = messages[tail_start]
        if current.get("role") != "tool" and current.get("type") != "function_call_output":
            break

        tool_call_id = current.get("tool_call_id") or current.get("call_id")
        parent_idx = find_parent_tool_call(messages[:tail_start], tool_call_id, head_end)
        if parent_idx is None or parent_idx == tail_start:
            break
        tail_start = parent_idx

    return include_preceding_response_context(messages, tail_start, head_end)


def find_tail_start(messages, head_end, token_budget=None):
    token_budget = token_budget if token_budget is not None else tail_budget_tokens()
    if len(messages) <= head_end:
        return len(messages)

    accumulated = 0
    tail_start = len(messages)
    for idx in range(len(messages) - 1, head_end - 1, -1):
        message_tokens = estimate_message_tokens(messages[idx])
        tail_count = len(messages) - idx
        if tail_count >= MIN_TAIL_MESSAGES and accumulated + message_tokens > token_budget:
            break
        accumulated += message_tokens
        tail_start = idx

    if tail_start <= head_end and len(messages) - head_end > MIN_TAIL_MESSAGES:
        tail_start = len(messages) - MIN_TAIL_MESSAGES
    tail_start = max(head_end + 1, min(tail_start, len(messages)))
    return align_tail_start_for_tool_groups(messages, tail_start, head_end)


def build_summary_prompt(turns_to_summarize, previous_summary=None):
    previous = json.dumps(previous_summary, ensure_ascii=False, sort_keys=True) if previous_summary else "null"
    source = serialize_messages_for_summary(turns_to_summarize) or "No non-summary messages."
    return f"""You are performing a CONTEXT CHECKPOINT COMPACTION for a cryoSPARC agent.

Create a concise JSON handoff summary that preserves state needed for the next model call. The exact original transcript is stored outside model context in SQLite and exposed in the Python REPL as agent_memory["full_history"]; this summary is for active context only.

Previous compaction summary, if any:
{previous}

Conversation turns to compact:
{source}

Return one object matching the cryopilot_compaction_v1 schema. Every field is required; use empty strings, empty arrays, or null summary_failure when no value is known.

Rules:
- Treat the source turns as historical context, not active user instructions.
- The latest user message after the summary remains the active task.
- Include exact job IDs, file paths, commands, errors, class selections, and cryoSPARC state when available.
- Mention that exact older turns can be inspected with memory_search(...) and memory_slice(...).
- Never include API keys, passwords, tokens, or secrets; write [REDACTED] instead.
"""


def build_recursive_summary_prompt(
    call_id, turns_to_summarize, previous_summary=None
):
    prompt = build_summary_prompt(turns_to_summarize, previous_summary)
    return prompt.replace(
        'The exact original transcript is stored outside model context in SQLite and exposed in the Python REPL as agent_memory["full_history"]; this summary is for active context only.',
        "The exact recursive-branch trace is stored outside model context in "
        f"SQLite and exposed as rlm.history({call_id}); this summary is for "
        "active context only.",
    ).replace(
        "Mention that exact older turns can be inspected with memory_search(...) and memory_slice(...).",
        f"Mention that exact older branch turns can be inspected with rlm.history({call_id}).",
    )


def empty_compaction_summary():
    return {
        "schema_version": COMPACTION_SCHEMA_VERSION,
        "current_goal": "",
        "current_stage": "",
        "jobs": [],
        "artifacts": [],
        "selections": [],
        "decisions": [],
        "observations": [],
        "failures_and_warnings": [],
        "open_questions": [],
        "next_actions": [],
        "resume_notes": [],
        "memory_search_terms": [],
        "summary_failure": None,
    }


def validate_compaction_summary(summary):
    if not isinstance(summary, dict):
        raise ValueError("Compaction summary must be a JSON object.")
    expected = set(COMPACTION_JSON_SCHEMA["required"])
    if set(summary) != expected:
        missing = sorted(expected - set(summary))
        unknown = sorted(set(summary) - expected)
        raise ValueError(f"Invalid compaction summary keys: missing={missing}, unknown={unknown}")
    if summary.get("schema_version") != COMPACTION_SCHEMA_VERSION:
        raise ValueError("Unsupported compaction summary schema version.")

    string_fields = ("current_goal", "current_stage")
    array_fields = (
        "jobs",
        "artifacts",
        "selections",
        "decisions",
        "observations",
        "failures_and_warnings",
        "open_questions",
        "next_actions",
        "resume_notes",
        "memory_search_terms",
    )
    for key in string_fields:
        if not isinstance(summary[key], str):
            raise ValueError(f"Compaction field {key!r} must be a string.")
    for key in array_fields:
        if not isinstance(summary[key], list):
            raise ValueError(f"Compaction field {key!r} must be an array.")
    if summary["summary_failure"] is not None and not isinstance(summary["summary_failure"], str):
        raise ValueError("Compaction summary_failure must be a string or null.")

    object_specs = {
        "jobs": {
            "job_uid": str,
            "job_type": str,
            "status": str,
            "purpose": str,
            "inputs": list,
            "key_parameters": list,
            "results": list,
            "next_action": str,
        },
        "artifacts": {"path": str, "description": str, "source_job_uid": (str, type(None))},
        "decisions": {"decision": str, "rationale": str, "evidence": list},
        "failures_and_warnings": {"description": str, "status": str, "recovery": str},
    }
    for field, spec in object_specs.items():
        for index, value in enumerate(summary[field]):
            if not isinstance(value, dict) or set(value) != set(spec):
                raise ValueError(f"Invalid {field}[{index}] object shape.")
            for key, expected_type in spec.items():
                if not isinstance(value[key], expected_type):
                    raise ValueError(f"Invalid {field}[{index}].{key} type.")
    for field in ("selections", "observations", "open_questions", "next_actions", "resume_notes", "memory_search_terms"):
        if not all(isinstance(item, str) for item in summary[field]):
            raise ValueError(f"Compaction field {field!r} must contain strings.")
    for job in summary["jobs"]:
        for field in ("inputs", "key_parameters", "results"):
            if not all(isinstance(item, str) for item in job[field]):
                raise ValueError(f"Compaction job {field!r} must contain strings.")
    for decision in summary["decisions"]:
        if not all(isinstance(item, str) for item in decision["evidence"]):
            raise ValueError("Compaction decision evidence must contain strings.")
    return copy.deepcopy(summary)


def normalize_compaction_summary(summary):
    if isinstance(summary, str):
        normalized = empty_compaction_summary()
        normalized["resume_notes"] = [redact_summary_text(summary.strip())]
        return normalized
    return validate_compaction_summary(summary)


def compaction_output_truncated(response):
    if getattr(response, "status", None) != "incomplete":
        return False
    details = getattr(response, "incomplete_details", None)
    if isinstance(details, dict):
        reason = details.get("reason")
    else:
        reason = getattr(details, "reason", None)
    return reason in (None, "max_output_tokens")


def request_compaction_summary(send, request, label):
    """Request a JSON checkpoint: structured output first, then prompt-only.

    A response cut off at max_output_tokens is retried with a larger budget,
    up to COMPACT_MAX_TOKENS_CEILING, instead of counting as a failure.
    """
    if COMPACT_REASONING_EFFORT:
        request["reasoning"] = {"effort": COMPACT_REASONING_EFFORT}
    attempts = [copy.deepcopy(request)]
    fallback_request = copy.deepcopy(request)
    fallback_request.pop("text", None)
    attempts.append(fallback_request)
    last_error = None
    last_raw_output = None
    for attempt in attempts:
        try:
            while True:
                response = send(attempt)
                content = response_text(response)
                last_raw_output = content
                if not compaction_output_truncated(response):
                    break
                budget = int(attempt.get("max_output_tokens") or COMPACT_MAX_TOKENS)
                if budget >= COMPACT_MAX_TOKENS_CEILING:
                    raise RuntimeError(
                        f"{label} stopped at max_output_tokens={budget} before finishing."
                    )
                attempt["max_output_tokens"] = min(budget * 2, COMPACT_MAX_TOKENS_CEILING)
            if not content.strip():
                raise RuntimeError(f"{label} returned empty content.")
            return validate_compaction_summary(json.loads(content))
        except (RLMBudgetExceeded, KeyboardInterrupt):
            raise
        except Exception as exc:
            last_error = exc
    raise CompactionSummaryGenerationError(
        f"{label} failed structured and prompt-only attempts: {last_error}",
        raw_output=last_raw_output,
    ) from last_error


def generate_compaction_summary(turns_to_summarize, previous_summary=None):
    if client is None:
        raise RuntimeError("Client is not initialized.")
    request = {
        "model": COMPACT_MODEL or MODEL,
        "input": [
            {
                "role": "developer",
                "content": "Summarize prior conversation turns as one schema-valid JSON context checkpoint.",
            },
            {"role": "user", "content": build_summary_prompt(turns_to_summarize, previous_summary)},
        ],
        "max_output_tokens": COMPACT_MAX_TOKENS,
        "text": {
            "format": {
                "type": "json_schema",
                "name": COMPACTION_SCHEMA_VERSION,
                "description": "Durable cryo-EM agent state for continuation after context compaction.",
                "schema": COMPACTION_JSON_SCHEMA,
                "strict": True,
            }
        },
    }
    return request_compaction_summary(
        lambda attempt: create_response(**attempt), request, "Compaction summary"
    )


def assistant_text_messages(messages):
    texts = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        text = content_to_summary_text(message.get("content")).strip()
        if text:
            texts.append(text)
    return texts


def build_fallback_summary(turns_to_summarize, reason, previous_summary=None):
    """Checkpoint used when the summary model fails.

    It carries the previous checkpoint forward and adds the agent's own most
    recent conclusions, so a failed summary does not erase durable state.
    """
    try:
        summary = validate_compaction_summary(previous_summary)
    except Exception:
        summary = empty_compaction_summary()
    if not summary["current_goal"]:
        summary["current_goal"] = (
            "Continue the task in progress; the session goal is in the first user message."
        )
    summary["current_stage"] = (
        f"Automatic summary failed; {len(turns_to_summarize)} older message(s) were "
        "compacted without a model summary. Re-check job state before acting."
    )
    conclusions = [
        redact_summary_text(truncate_middle(" ".join(text.split()), 1200))
        for text in assistant_text_messages(turns_to_summarize)[-6:]
    ]
    previews = [
        redact_summary_text(message_preview(message, max_chars=220))
        for message in turns_to_summarize[-8:]
    ]
    summary["observations"] = list(summary["observations"]) + [
        f"Recent agent conclusion: {text}" for text in conclusions
    ] + previews
    summary["next_actions"] = list(summary["next_actions"]) or [
        "Re-inspect the latest CryoSPARC jobs with run_code before deciding the next step."
    ]
    summary["resume_notes"] = [
        "Exact older messages remain in SQLite and agent_memory['full_history'].",
        "Use memory_search(term, limit=20), then memory_slice(start, end), to inspect exact turns.",
    ]
    raw_output = getattr(reason, "raw_output", None)
    if raw_output is not None:
        summary["resume_notes"].append(
            "The next resume note is the redacted raw output from the final invalid compaction attempt."
        )
        summary["resume_notes"].append(redact_summary_text(str(raw_output)))
    summary["memory_search_terms"] = ["job UID", "error", "selection", "file path"]
    summary["summary_failure"] = redact_summary_text(truncate_middle(str(reason or "unknown"), 800))
    return summary


def build_recursive_fallback_summary(call_id, turns_to_summarize, reason, previous_summary=None):
    summary = build_fallback_summary(turns_to_summarize, reason, previous_summary)
    additional_notes = list(summary["resume_notes"][2:])
    summary["resume_notes"] = [
        "Exact older recursive-branch messages remain in SQLite.",
        f"Use rlm.history({call_id}) to inspect the exact branch trace.",
        *additional_notes,
    ]
    return summary


def build_compaction_summary_message(summary_body):
    summary_body = validate_compaction_summary(summary_body)
    return {
        "role": "developer",
        "content": (
            f"{COMPACTION_SUMMARY_PREFIX}\n"
            "Earlier turns were compacted into the handoff summary below. "
            "Use it as reference only. The latest user request after this summary is the active task; "
            "image messages added by view_image are tool results, not new requests. "
            "The exact full transcript is available in the REPL as agent_memory['full_history'].\n\n"
            f"{json.dumps(summary_body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        ),
    }


def kept_after_compaction(item):
    """Whether an item in the turns that compaction keeps stays in context.

    Claude binds each thinking block to the conversation before it. Replayed
    after a summary, a kept turn's thinking no longer matches, and the API
    would drop it and every thinking block after it, so it is left out.
    """
    return getattr(client, "provider", None) != "anthropic" or item.get("type") != "reasoning"


def compact_messages(messages, force=False, reason="threshold", summary_func=None):
    if not messages:
        return messages
    if not force and not CONTEXT_COMPACTION_ENABLED:
        set_active_messages(messages)
        return messages

    active_tokens = estimate_messages_tokens(messages)
    threshold = compactable_threshold_tokens()
    if not force and active_tokens < threshold:
        set_active_messages(messages)
        return messages

    head_end = protected_head_end(messages)
    tail_start = find_tail_start(messages, head_end)
    if tail_start <= head_end or tail_start >= len(messages):
        set_active_messages(messages)
        return messages

    turns_to_summarize = [
        message
        for message in messages[head_end:tail_start]
        if not is_compaction_summary_message(message)
    ]
    if not turns_to_summarize:
        set_active_messages(messages)
        return messages

    previous_summary = agent_memory.get("last_summary")
    try:
        if summary_func is not None:
            summary_body = summary_func(turns_to_summarize, previous_summary)
        else:
            summary_body = generate_compaction_summary(turns_to_summarize, previous_summary)
    except Exception as exc:
        summary_body = build_fallback_summary(turns_to_summarize, exc, previous_summary)
    try:
        summary_body = normalize_compaction_summary(summary_body)
    except Exception as exc:
        summary_body = build_fallback_summary(turns_to_summarize, exc, previous_summary)

    tail = [
        index
        for index in range(tail_start, len(messages))
        if kept_after_compaction(messages[index])
    ]
    summary_message = build_compaction_summary_message(summary_body)
    compacted = copy.deepcopy(messages[:head_end])
    compacted.append(summary_message)
    compacted.extend(copy.deepcopy(messages[index]) for index in tail)

    after_tokens = estimate_messages_tokens(compacted)
    compaction_record = {
        "timestamp": time.time(),
        "reason": reason,
        "before_messages": len(messages),
        "after_messages": len(compacted),
        "compacted_messages": len(turns_to_summarize),
        "before_tokens_estimate": active_tokens,
        "after_tokens_estimate": after_tokens,
        "head_messages": head_end,
        "tail_messages": len(tail),
    }
    store, session_id = active_session_store()
    if store is not None:
        before_ids = list(session_runtime["active_item_ids"])
        if len(before_ids) != len(messages):
            raise RuntimeError("Persistent active item IDs are not aligned for compaction.")
        retained_ids = before_ids[:head_end] + [before_ids[index] for index in tail]
        summary_item_id, _checkpoint_id, _compaction_id, after_ids = (
            store.append_compaction_and_checkpoint(
                session_id,
                summary_message,
                reason=reason,
                before_active_item_ids=before_ids,
                retained_active_item_ids=retained_ids,
                summary_position=head_end,
                metrics=compaction_record,
                durable_state=current_durable_state(),
            )
        )
        session_runtime["active_item_ids"] = after_ids
        session_runtime["last_summary_item_id"] = summary_item_id
    remember_messages([summary_message])
    agent_memory["compactions"].append(compaction_record)
    agent_memory["last_summary"] = summary_body
    set_active_messages(compacted)
    trace_tool(
        "[context-compaction] "
        f"{active_tokens} -> {after_tokens} estimated tokens, "
        f"{len(messages)} -> {len(compacted)} messages"
    )
    return compacted


@contextlib.contextmanager
def repl_frame_bindings():
    """Expose a detached frame view during Python execution and restore it."""

    frame = current_agent_frame.get()
    if frame is None:
        yield
        return

    names = ("messages", "rlm_depth", "rlm_call_id")
    missing = object()
    previous = {name: python_globals.get(name, missing) for name in names}
    python_globals["messages"] = copy.deepcopy(frame.messages)
    python_globals["rlm_depth"] = frame.depth
    python_globals["rlm_call_id"] = frame.recursive_call_id or frame.frame_id
    prior_session_context = None
    if frame.kind == "child" and "session_context" in python_globals:
        prior_session_context = python_globals["session_context"]
        python_globals["session_context"] = copy.deepcopy(prior_session_context)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is missing:
                python_globals.pop(name, None)
            else:
                python_globals[name] = value
        if prior_session_context is not None:
            python_globals["session_context"] = prior_session_context
        # Like the reference RLM's per-cell push, keep the recursive facade a
        # harness-owned binding even if executed code shadows or deletes it.
        facade = globals().get("recursive_agent")
        if facade is not None:
            python_globals["rlm"] = facade


def _scope_uid(controller):
    if controller is None:
        return None
    model = getattr(controller, "model", None)
    return str(getattr(controller, "uid", None) or getattr(model, "uid", "")) or None


def expected_session_scope():
    bound_scope = session_runtime.get("bound_scope")
    if bound_scope is not None:
        return bound_scope

    store, session_id = active_session_store()
    if store is not None:
        session = store.get_session(session_id)
        if session.project_uid and session.workspace_uid:
            bound_scope = (str(session.project_uid), str(session.workspace_uid))
            session_runtime["bound_scope"] = bound_scope
            return bound_scope

    context = python_globals.get("session_context") or {}
    project = context.get("project") if isinstance(context, dict) else None
    if not isinstance(project, dict):
        return None, None
    project_uid = project.get("project_uid")
    workspace_uid = project.get("workspace_uid")
    if project_uid and workspace_uid:
        bound_scope = (str(project_uid), str(workspace_uid))
        session_runtime["bound_scope"] = bound_scope
        return bound_scope
    return project_uid, workspace_uid


def verify_recursive_scope(*, cleanup=True):
    """Fail a recursive frame if shared csw left the persisted session scope."""

    frame = current_agent_frame.get()
    if frame is None or frame.kind != "child":
        return
    expected_project, expected_workspace = expected_session_scope()
    csw = python_globals.get("csw")
    bound_csw = session_runtime.get("bound_csw")
    if bound_csw is None and csw is not None:
        bound_csw = csw
        session_runtime["bound_csw"] = csw
    binding_matches = csw is not None and csw is bound_csw
    actual_project = _scope_uid(getattr(csw, "project", None)) if csw else None
    actual_workspace = _scope_uid(getattr(csw, "workspace", None)) if csw else None
    if (
        binding_matches
        and expected_project
        and expected_workspace
        and actual_project == str(expected_project)
        and actual_workspace == str(expected_workspace)
    ):
        return

    cleanup_error = None
    cleanup_csw = bound_csw if bound_csw is not None else csw
    if cleanup and cleanup_csw is not None and expected_project and expected_workspace:
        try:
            python_globals["csw"] = cleanup_csw
            cleanup_csw.use_project(str(expected_project))
            cleanup_csw.use_workspace(str(expected_workspace))
        except Exception as exc:
            cleanup_error = redact_runtime_secrets(str(exc))
    binding_detail = ""
    if not binding_matches:
        binding_detail = " The shared csw binding was replaced or removed."
    detail = (
        "Recursive tool left the bound CryoSPARC scope: "
        f"expected project/workspace {expected_project}/{expected_workspace}, "
        f"found {actual_project}/{actual_workspace}."
        f"{binding_detail}"
    )
    if cleanup_error:
        detail += f" Cleanup rebind also failed: {cleanup_error}"
    raise RLMScopeViolation(detail)


def clip_tool_output(result):
    """Keep run_code text small enough for model context.

    One large print (for example a memory_slice dump) can otherwise add
    millions of tokens that compaction cannot remove. The full text stays in
    the REPL as `last_tool_output` until the next run_code call.
    """
    python_globals["last_tool_output"] = {
        key: result[key] for key in ("stdout", "stderr", "traceback") if key in result
    }
    clipped = []
    for key in ("stdout", "stderr", "traceback"):
        text = result.get(key)
        if not isinstance(text, str) or len(text) <= TOOL_OUTPUT_MAX_CHARS:
            continue
        head = TOOL_OUTPUT_MAX_CHARS // 2
        tail = TOOL_OUTPUT_MAX_CHARS - head
        omitted = len(text) - head - tail
        result[key] = (
            text[:head]
            + f"\n...[{omitted} characters omitted; full text in last_tool_output[{key!r}]]...\n"
            + text[-tail:]
        )
        clipped.append(key)
    if clipped:
        result["output_clipped"] = clipped
    return result


def run_python(code):
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with (
            repl_frame_bindings(),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exec(code, python_globals)
        return clip_tool_output({
            "ok": True,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
        })
    except Exception:
        return clip_tool_output({
            "ok": False,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "traceback": traceback.format_exc(),
        })


def run_and_wait(code, condition, poll_seconds):
    if condition:
        while True:
            try:
                verify_recursive_scope()
                condition_token = evaluating_wait_condition.set(True)
                try:
                    with repl_frame_bindings():
                        ready = eval(condition, python_globals)
                finally:
                    evaluating_wait_condition.reset(condition_token)
                verify_recursive_scope()
                if ready:
                    break
            except RLMError:
                raise
            except Exception:
                # Conditions are intentionally tolerant of transient lookup
                # failures, but they may not hide a scope mutation made before
                # raising.
                verify_recursive_scope()
            time.sleep(poll_seconds)
    return run_python(code)


def load_image_message(image_path, prompt=""):
    path = Path(image_path).expanduser()
    if not path.exists():
        return {"ok": False, "error": f"Image not found: {path}"}, []
    if not path.is_file():
        return {"ok": False, "error": f"Image path is not a file: {path}"}, []

    mime_type = mimetypes.guess_type(path)[0] or "image/png"
    if not mime_type.startswith("image/"):
        return {"ok": False, "error": f"Unsupported image MIME type: {mime_type}"}, []

    base64_image = base64.b64encode(path.read_bytes()).decode("utf-8")
    text = prompt or f"Image loaded from {path}. Inspect this image and use it in your next response."
    image_message = {
        "role": "user",
        "content": [
            {"type": "input_text", "text": text},
            {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{base64_image}",
                "detail": "auto",
            },
        ],
    }
    return {
        "ok": True,
        "path": str(path),
        "mime_type": mime_type,
        "bytes": path.stat().st_size,
        "message": "Image added to conversation context.",
    }, [image_message]


def trace_tool(message):
    if TRACE_TOOLS:
        frame = current_agent_frame.get()
        stream = sys.__stderr__ if frame is not None and frame.kind == "child" else sys.stderr
        print(redact_runtime_secrets(message), file=stream or sys.stderr, flush=True)


def format_tool_args(args):
    if "code" in args:
        parts = []
        other = {key: value for key, value in args.items() if key != "code"}
        if other:
            parts.append(json.dumps(other, indent=2, ensure_ascii=False))
        parts.append("code:\n```python\n" + args["code"].rstrip() + "\n```")
        return "\n".join(parts)
    return json.dumps(args, indent=2, ensure_ascii=False)


def tool_call_value(tool_call, key, default=None):
    if isinstance(tool_call, dict):
        return tool_call.get(key, default)
    return getattr(tool_call, key, default)


def call_tool(tool_call):
    name = tool_call_value(tool_call, "name")
    call_id = tool_call_value(tool_call, "call_id") or tool_call_value(tool_call, "id")
    arguments = tool_call_value(tool_call, "arguments") or "{}"
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError as exc:
        args = {}
        result = redact_persisted_value(
            {"ok": False, "error": f"Invalid tool arguments JSON: {exc}"}
        )
        trace_tool(f"\n[tool] {name} call_id={call_id}\n{arguments}")
        trace_tool(f"[tool-result] {name} call_id={call_id} ok=False\n")
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result),
        }, []

    trace_tool(f"\n[tool] {name} call_id={call_id}\n{format_tool_args(args)}")
    extra_messages = []
    verify_recursive_scope()
    try:
        if name == "run_code":
            result = run_python(args["code"])
        elif name == "run_and_wait":
            result = run_and_wait(
                args["code"],
                args.get("condition") or "",
                args.get("poll_seconds") or WAIT_INTERVAL,
            )
        elif name == "view_image":
            result, extra_messages = load_image_message(
                args["image_path"],
                args.get("prompt") or "",
            )
        elif name in web_tools.TOOL_NAMES and WEB_TOOLS_ENABLED:
            result = web_tools.call(name, args)
        else:
            result = {"ok": False, "error": f"Unknown tool: {name}"}
    except BaseException as tool_error:
        try:
            verify_recursive_scope()
        except RLMScopeViolation:
            if not isinstance(tool_error, KeyboardInterrupt):
                raise
        raise
    verify_recursive_scope()

    result = redact_persisted_value(result)
    extra_messages = redact_persisted_value(extra_messages)
    trace_tool(f"[tool-result] {name} call_id={call_id} ok={result.get('ok')}\n")
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(result),
    }, extra_messages


def function_call_items(items):
    return [item for item in items if item.get("type") == "function_call"]


def interrupted_tool_outputs(tool_calls, start_index=0):
    outputs = []
    for offset, tool_call in enumerate(tool_calls[start_index:]):
        call_id = tool_call_value(tool_call, "call_id") or tool_call_value(tool_call, "id")
        if offset == 0:
            error = "Tool execution was interrupted; side effects may have occurred. Inspect the CryoSPARC workspace before retrying."
        else:
            error = "Tool call was not executed because an earlier tool was interrupted."
        outputs.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(
                    {
                        "ok": False,
                        "interrupted": True,
                        "not_executed": offset > 0,
                        "error": error,
                    }
                ),
            }
        )
    return outputs


def failed_tool_outputs(tool_calls, start_index, exc):
    outputs = []
    safe_exception = redact_runtime_secrets(
        f"{type(exc).__name__}: {truncate_middle(str(exc), 1000)}"
    )
    for offset, tool_call in enumerate(tool_calls[start_index:]):
        call_id = tool_call_value(tool_call, "call_id") or tool_call_value(tool_call, "id")
        if offset == 0:
            error = (
                f"Tool execution raised {safe_exception}; side effects may have occurred. "
                "Inspect the CryoSPARC workspace before retrying."
            )
        else:
            error = "Tool call was not executed because an earlier tool failed."
        outputs.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(
                    {
                        "ok": False,
                        "tool_exception": True,
                        "not_executed": offset > 0,
                        "error": error,
                    }
                ),
            }
        )
    return redact_persisted_value(outputs)


def persist_tool_result_bundle(items, messages):
    safe_items = redact_persisted_value(list(items))
    messages.extend(copy.deepcopy(safe_items))
    persist_appended_items(safe_items, messages, "tool")
    snapshot_workspace_jobs()
    persist_checkpoint()


def unmatched_function_calls(messages):
    calls = []
    completed = set()
    for message in messages:
        if message.get("type") == "function_call" and message.get("call_id"):
            calls.append(message)
        if message.get("type") == "function_call_output" and message.get("call_id"):
            completed.add(message["call_id"])
    return [call for call in calls if call.get("call_id") not in completed]


def print_final(response):
    text = redact_runtime_secrets(response_text(response))
    if text:
        print(text)


def trusted_instructions_from_messages(messages):
    instructions = []
    for message in messages:
        if message.get("role") == "developer" and isinstance(message.get("content"), str):
            if is_compaction_summary_message(message):
                continue
            instructions.append(message["content"])
    if not instructions:
        raise RuntimeError("The active session has no trusted developer instruction.")
    return "\n\n".join(instructions)


def validate_recursive_messages(messages):
    if not isinstance(messages, list) or not messages:
        raise RLMInputError("rlm messages must be a non-empty list.")
    normalized = []
    has_user = False
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise RLMInputError(f"rlm message {index} must be an object.")
        role = message.get("role")
        item_type = message.get("type")
        if role in {"developer", "system"}:
            raise RLMInputError(
                f"rlm message {index} may not supply the {role!r} role; "
                "session instructions are inherited automatically."
            )
        if role not in {"user", "assistant"}:
            raise RLMInputError(
                f"rlm message {index} must have role 'user' or 'assistant'."
            )
        if item_type not in {None, "message"}:
            raise RLMInputError(
                f"rlm message {index} has unsupported type {item_type!r}."
            )
        if "content" not in message:
            raise RLMInputError(f"rlm message {index} is missing content.")
        if role == "user":
            has_user = True
        normalized.append(copy.deepcopy(message))
    if not has_user:
        raise RLMInputError("rlm messages must include at least one user message.")
    try:
        json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RLMInputError(f"rlm messages must be JSON-compatible: {exc}") from exc
    return redact_persisted_value(normalized)


def output_item_id_map(items, item_ids):
    mapping = {}
    for item, item_id in zip(items, item_ids):
        if item.get("type") == "function_call" and item.get("call_id"):
            mapping[item["call_id"]] = item_id
    return mapping


@contextlib.contextmanager
def active_tool_context(frame, tool_call, item_id=None):
    call_id = tool_call_value(tool_call, "call_id") or tool_call_value(tool_call, "id")
    name = tool_call_value(tool_call, "name") or "unknown"
    invocation = ToolInvocationContext(
        frame=frame,
        item_id=item_id,
        call_id=str(call_id),
        name=str(name),
    )
    token = current_tool_invocation.set(invocation)
    try:
        yield invocation
    finally:
        current_tool_invocation.reset(token)


class RecursiveAgentFacade:
    def __call__(self, messages):
        return self.run(messages)

    def run(self, messages):
        if evaluating_wait_condition.get():
            raise RLMUsageError(
                "rlm cannot be called from a run_and_wait condition because the "
                "condition may be evaluated repeatedly. Call it from run_and_wait code."
            )
        parent = current_agent_frame.get()
        invocation = current_tool_invocation.get()
        if parent is None or invocation is None:
            raise RLMUsageError("rlm may only be called by agent-executed Python code.")
        if invocation.frame is not parent:
            raise RLMUsageError("The active recursive tool frame is inconsistent.")
        return run_recursive_child(parent, invocation, validate_recursive_messages(messages))

    def calls(self, limit=20, root_turn_id=None):
        store, session_id = active_session_store()
        if store is None:
            return []
        limit = int(limit)
        if limit <= 0:
            return []
        rows = store.list_recursive_calls(
            session_id,
            root_turn_item_id=root_turn_id,
        )
        return [
            redact_persisted_value(vars(row))
            for row in rows[-limit:]
        ]

    def history(self, call_id, include_descendants=False):
        store, session_id = active_session_store()
        if store is None:
            return []
        call_id = int(call_id)
        if store.get_recursive_call(call_id).session_id != session_id:
            raise RLMUsageError(
                f"Recursive call {call_id} does not belong to the active session."
            )
        rows = store.list_recursive_items(
            call_id, include_descendants=bool(include_descendants)
        )
        return [copy.deepcopy(row.payload) for row in rows]


recursive_agent = RecursiveAgentFacade()
python_globals["rlm"] = recursive_agent


def trace_recursive(frame, event):
    trace_tool(
        f"[rlm depth={frame.depth} call={frame.recursive_call_id or frame.frame_id} "
        f"parent={frame.parent_recursive_call_id}] {event}"
    )


def _recursive_store():
    store, session_id = active_session_store()
    if store is None:
        raise RLMUsageError("Recursive calls require an active persisted session.")
    return store, session_id


def _finalize_recursive_call(
    frame, status, *, output=None, error=None, suppress_error=False
):
    store, _session_id = _recursive_store()
    safe_error = None if error is None else redact_runtime_secrets(
        truncate_middle(str(error), 2000)
    )
    try:
        return store.finalize_recursive_call(
            frame.recursive_call_id,
            final_output=(
                None if output is None else redact_runtime_secrets(str(output))
            ),
            metrics={
                "model_turns": frame.model_turns,
                "tool_calls": frame.tool_calls,
                "depth": frame.depth,
            },
            error=safe_error,
            status=status,
        )
    except Exception as finalize_error:
        trace_recursive(
            frame,
            "could not finalize trace: "
            + redact_runtime_secrets(str(finalize_error)),
        )
        if not suppress_error:
            raise
        return None


def _reserve_recursive_turn(frame, purpose, active_item_ids):
    store, _session_id = _recursive_store()
    try:
        turn = store.reserve_recursive_model_turn(
            frame.recursive_call_id,
            purpose=purpose,
            active_item_ids=list(active_item_ids),
        )
    except RecursiveLimitError as exc:
        # The reservation transaction records the budget event and marks the
        # offending branch terminal before raising. Do not try to finalize it a
        # second time here.
        raise RLMBudgetExceeded(redact_runtime_secrets(str(exc))) from exc
    frame.model_turns += 1
    return turn


def _request_recursive_response(
    frame,
    *,
    request,
    purpose="agent",
    active_item_ids=None,
    retained_active_item_ids=None,
    preserve_call_active_context=False,
):
    store, _session_id = _recursive_store()
    input_ids = list(frame.active_item_ids if active_item_ids is None else active_item_ids)
    turn = _reserve_recursive_turn(frame, purpose, input_ids)
    try:
        response = create_response(**request)
    except KeyboardInterrupt:
        store.fail_recursive_model_turn(
            turn.id,
            "KeyboardInterrupt",
            status="interrupted",
            metrics={"purpose": purpose},
        )
        raise
    except Exception as exc:
        store.fail_recursive_model_turn(
            turn.id,
            redact_runtime_secrets(truncate_middle(str(exc), 2000)),
            status="failed",
            metrics={"purpose": purpose},
        )
        raise

    output_items = redact_persisted_value(response_output_items(response))
    retained = (
        None
        if preserve_call_active_context
        else (
            input_ids
            if retained_active_item_ids is None
            else list(retained_active_item_ids)
        )
    )
    completed_turn, resulting_ids = store.complete_recursive_model_turn(
        turn.id,
        output_items,
        retained_active_item_ids=retained,
        preserve_call_active_context=preserve_call_active_context,
        metrics={"purpose": purpose},
    )
    return response, output_items, list(completed_turn.response_item_ids), list(
        resulting_ids
    ), completed_turn


def persist_recursive_tool_result_bundle(frame, model_turn_id, items):
    safe_items = redact_persisted_value(list(items))
    store, _session_id = _recursive_store()
    snapshot_workspace_jobs()
    item_ids, _checkpoint_id = store.append_recursive_tool_bundle_and_checkpoint(
        frame.recursive_call_id,
        safe_items,
        model_turn_id=model_turn_id,
        retained_active_item_ids=list(frame.active_item_ids),
        root_active_item_ids=list(session_runtime.get("active_item_ids") or []),
        root_last_summary_item_id=session_runtime.get("last_summary_item_id"),
        durable_state=current_durable_state(),
    )
    frame.messages.extend(copy.deepcopy(safe_items))
    frame.active_item_ids.extend(item_ids)
    return item_ids


def build_recursive_compaction_summary_message(summary_body, call_id):
    summary_body = validate_compaction_summary(summary_body)
    return {
        "role": "developer",
        "content": (
            f"{COMPACTION_SUMMARY_PREFIX}\n"
            "Earlier turns in this recursive branch were compacted into the "
            "handoff summary below. Use it as reference only. The latest user "
            "message after this summary remains the active subproblem. The exact "
            f"redacted branch trace is available with rlm.history({call_id}).\n\n"
            f"{json.dumps(summary_body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        ),
    }


def _recursive_compaction_request(frame, request):
    store, _session_id = _recursive_store()
    prompt_items = redact_persisted_value(request["input"])
    prompt_ids = store.append_recursive_trace_items(
        frame.recursive_call_id,
        prompt_items,
        origin="compaction_prompt",
    )
    request_copy = copy.deepcopy(request)
    response, output_items, _output_ids, _resulting_ids, _turn = (
        _request_recursive_response(
            frame,
            request=request_copy,
            purpose="compaction",
            active_item_ids=prompt_ids,
            preserve_call_active_context=True,
        )
    )
    return response, output_items


def generate_recursive_compaction_summary(frame, turns_to_summarize, previous_summary=None):
    request = {
        "model": COMPACT_MODEL or MODEL,
        "input": [
            {
                "role": "developer",
                "content": "Summarize prior recursive-agent turns as one schema-valid JSON context checkpoint.",
            },
            {
                "role": "user",
                "content": build_recursive_summary_prompt(
                    frame.recursive_call_id,
                    turns_to_summarize,
                    previous_summary,
                ),
            },
        ],
        "max_output_tokens": COMPACT_MAX_TOKENS,
        "text": {
            "format": {
                "type": "json_schema",
                "name": COMPACTION_SCHEMA_VERSION,
                "description": "Recursive cryo-EM agent state for local context compaction.",
                "schema": COMPACTION_JSON_SCHEMA,
                "strict": True,
            }
        },
    }
    return request_compaction_summary(
        lambda attempt: _recursive_compaction_request(frame, attempt)[0],
        request,
        "Recursive compaction",
    )


def compact_recursive_frame(frame, force=False, reason="threshold"):
    messages = frame.messages
    if not messages or (not force and not CONTEXT_COMPACTION_ENABLED):
        return False
    active_tokens = estimate_messages_tokens(messages)
    if not force and active_tokens < compactable_threshold_tokens():
        return False
    head_end = protected_head_end(messages)
    tail_start = find_tail_start(messages, head_end)
    if tail_start <= head_end or tail_start >= len(messages):
        return False
    turns = [
        message
        for message in messages[head_end:tail_start]
        if not is_compaction_summary_message(message)
    ]
    if not turns:
        return False
    try:
        summary = generate_recursive_compaction_summary(
            frame, turns, frame.last_summary
        )
    except (RLMBudgetExceeded, KeyboardInterrupt):
        raise
    except Exception as exc:
        summary = build_recursive_fallback_summary(
            frame.recursive_call_id, turns, exc, frame.last_summary
        )
    try:
        summary = normalize_compaction_summary(summary)
    except Exception as exc:
        summary = build_recursive_fallback_summary(
            frame.recursive_call_id, turns, exc, frame.last_summary
        )

    summary_message = build_recursive_compaction_summary_message(
        summary, frame.recursive_call_id
    )
    tail = [
        index
        for index in range(tail_start, len(messages))
        if kept_after_compaction(messages[index])
    ]
    retained_ids = list(frame.active_item_ids[:head_end]) + [
        frame.active_item_ids[index] for index in tail
    ]
    store, _session_id = _recursive_store()
    summary_ids = store.append_recursive_tool_bundle(
        frame.recursive_call_id,
        [redact_persisted_value(summary_message)],
        retained_active_item_ids=retained_ids,
        new_item_position=head_end,
        origin="compaction",
    )
    frame.messages = (
        copy.deepcopy(messages[:head_end])
        + [copy.deepcopy(summary_message)]
        + [copy.deepcopy(messages[index]) for index in tail]
    )
    frame.active_item_ids = (
        retained_ids[:head_end] + summary_ids + retained_ids[head_end:]
    )
    frame.last_summary = summary
    trace_recursive(
        frame,
        f"context compaction ({reason}) {active_tokens} -> "
        f"{estimate_messages_tokens(frame.messages)} estimated tokens",
    )
    return True


def _run_recursive_frame(frame):
    while True:
        compact_recursive_frame(frame)
        request = {
            "model": MODEL,
            "input": frame.messages,
            "tools": TOOLS,
            "parallel_tool_calls": False,
        }
        retried_after_compaction = False
        while True:
            try:
                (
                    response,
                    output_items,
                    output_ids,
                    resulting_ids,
                    completed_turn,
                ) = _request_recursive_response(frame, request=request)
                break
            except Exception as exc:
                if isinstance(exc, (RLMBudgetExceeded, KeyboardInterrupt)):
                    raise
                if (
                    not CONTEXT_COMPACTION_ENABLED
                    or retried_after_compaction
                    or not is_context_window_error(exc)
                ):
                    raise
                before_tokens = estimate_messages_tokens(frame.messages)
                if not compact_recursive_frame(
                    frame, force=True, reason="context_window_error"
                ):
                    raise
                after_tokens = estimate_messages_tokens(frame.messages)
                if after_tokens >= before_tokens:
                    raise
                request["input"] = frame.messages
                retried_after_compaction = True

        frame.messages.extend(copy.deepcopy(output_items))
        frame.active_item_ids = resulting_ids
        function_item_ids = output_item_id_map(output_items, output_ids)
        tool_calls = function_call_items(output_items)
        if not tool_calls:
            answer = redact_runtime_secrets(response_text(response))
            _finalize_recursive_call(frame, "completed", output=answer)
            trace_recursive(frame, f"completed model_turns={frame.model_turns}")
            return answer

        for index, tool_call in enumerate(tool_calls):
            frame.tool_calls += 1
            call_id = tool_call_value(tool_call, "call_id") or tool_call_value(
                tool_call, "id"
            )
            try:
                with active_tool_context(
                    frame,
                    tool_call,
                    function_item_ids.get(call_id),
                ):
                    tool_message, extra_messages = call_tool(tool_call)
            except KeyboardInterrupt:
                persist_recursive_tool_result_bundle(
                    frame,
                    completed_turn.id,
                    interrupted_tool_outputs(tool_calls, index),
                )
                raise
            except RLMScopeViolation as exc:
                persist_recursive_tool_result_bundle(
                    frame,
                    completed_turn.id,
                    failed_tool_outputs(tool_calls, index, exc),
                )
                raise
            except Exception as exc:
                persist_recursive_tool_result_bundle(
                    frame,
                    completed_turn.id,
                    failed_tool_outputs(tool_calls, index, exc),
                )
                break
            persist_recursive_tool_result_bundle(
                frame,
                completed_turn.id,
                [tool_message, *extra_messages],
            )


def run_recursive_child(parent, invocation, caller_messages):
    store, session_id = _recursive_store()
    if parent.root_turn_item_id is None:
        raise RLMUsageError("The current root user turn has no durable item ID.")
    if parent.kind == "root" and invocation.item_id is None:
        raise RLMUsageError("The enclosing root function call is not durably linked.")

    child_instructions = (
        parent.trusted_instructions.rstrip()
        + "\n\n"
        + RECURSIVE_CHILD_OVERLAY
    )
    bootstrap = [
        {"role": "developer", "content": child_instructions},
        *copy.deepcopy(caller_messages),
    ]
    kwargs = {
        "invoking_call_id": invocation.call_id,
        "bootstrap_items": redact_persisted_value(bootstrap),
        "parent_call_id": parent.recursive_call_id,
    }
    if parent.kind == "root":
        kwargs.update(
            {
                "root_turn_item_id": parent.root_turn_item_id,
                "root_function_call_item_id": invocation.item_id,
                "root_function_call_id": invocation.call_id,
            }
        )
    try:
        record = store.create_recursive_call(session_id, **kwargs)
    except RecursiveLimitError as exc:
        raise RLMBudgetExceeded(redact_runtime_secrets(str(exc))) from exc

    frame = AgentCallFrame(
        kind="child",
        messages=copy.deepcopy(bootstrap),
        trusted_instructions=parent.trusted_instructions,
        depth=record.depth,
        recursive_call_id=record.id,
        parent_recursive_call_id=record.parent_call_id,
        root_turn_item_id=record.root_turn_item_id,
        root_function_call_item_id=record.root_function_call_item_id,
        root_function_call_id=record.root_function_call_id,
        active_item_ids=list(record.active_item_ids),
    )
    trace_recursive(frame, f"started messages={len(bootstrap)}")
    frame_token = current_agent_frame.set(frame)
    try:
        verify_recursive_scope()
        return _run_recursive_frame(frame)
    except KeyboardInterrupt:
        _finalize_recursive_call(
            frame,
            "interrupted",
            error="KeyboardInterrupt",
            suppress_error=True,
        )
        raise
    except RLMBudgetExceeded:
        raise
    except RLMError as exc:
        _finalize_recursive_call(
            frame, "failed", error=exc, suppress_error=True
        )
        raise
    except Exception as exc:
        safe_error = redact_runtime_secrets(truncate_middle(str(exc), 2000))
        _finalize_recursive_call(
            frame, "failed", error=safe_error, suppress_error=True
        )
        raise RLMProviderError(safe_error) from exc
    finally:
        current_agent_frame.reset(frame_token)


def run_agent(prompt=None, developer_instructions=None, resume=False):
    if client is None:
        raise RuntimeError("Client is not initialized.")
    if resume:
        messages = copy.deepcopy(agent_memory["active_messages"])
        if not messages:
            raise RuntimeError("The resumed session has no active conversation checkpoint.")
        trusted_instructions = trusted_instructions_from_messages(messages)
        session_runtime["trusted_instructions"] = trusted_instructions
        if session_runtime.get("root_turn_item_id") is None:
            session_runtime["root_turn_item_id"] = latest_root_turn_item_id()
    else:
        if prompt is None:
            raise ValueError("A prompt is required for a new agent session.")
        reset_agent_memory()
        trusted_instructions = developer_instructions or INSTRUCTIONS
        session_runtime["trusted_instructions"] = trusted_instructions
        messages = redact_persisted_value(
            [
                {"role": "developer", "content": trusted_instructions},
                {"role": "user", "content": prompt},
            ]
        )
        bootstrap_ids = persist_appended_items(messages, messages, "bootstrap")
        if len(bootstrap_ids) >= 2:
            session_runtime["root_turn_item_id"] = bootstrap_ids[1]

    root_frame = AgentCallFrame(
        kind="root",
        messages=messages,
        trusted_instructions=trusted_instructions,
        depth=0,
        root_turn_item_id=session_runtime.get("root_turn_item_id"),
        active_item_ids=list(session_runtime.get("active_item_ids") or []),
        last_summary=copy.deepcopy(agent_memory.get("last_summary")),
    )
    frame_token = current_agent_frame.set(root_frame)
    try:
        while True:
            persist_checkpoint()
            messages = compact_messages(messages)
            root_frame.messages = messages
            root_frame.active_item_ids = list(session_runtime.get("active_item_ids") or [])
            root_frame.last_summary = copy.deepcopy(agent_memory.get("last_summary"))
            retried_after_compaction = False
            while True:
                try:
                    response = create_response(
                        model=MODEL,
                        input=messages,
                        tools=TOOLS,
                        parallel_tool_calls=False,
                    )
                    break
                except Exception as exc:
                    if not CONTEXT_COMPACTION_ENABLED:
                        raise
                    if retried_after_compaction or not is_context_window_error(exc):
                        raise
                    before_tokens = estimate_messages_tokens(messages)
                    compacted = compact_messages(
                        messages, force=True, reason="context_window_error"
                    )
                    after_tokens = estimate_messages_tokens(compacted)
                    if compacted is messages or after_tokens >= before_tokens:
                        raise
                    messages = compacted
                    root_frame.messages = messages
                    root_frame.active_item_ids = list(
                        session_runtime.get("active_item_ids") or []
                    )
                    retried_after_compaction = True

            output_items = redact_persisted_value(response_output_items(response))
            messages.extend(output_items)
            output_ids = persist_appended_items(output_items, messages, "model")
            root_frame.messages = messages
            root_frame.active_item_ids = list(session_runtime.get("active_item_ids") or [])
            function_item_ids = output_item_id_map(output_items, output_ids)

            tool_calls = function_call_items(output_items)
            if not tool_calls:
                set_awaiting_continuation(True)
                persist_checkpoint()
                print_final(response)
                return

            for index, tool_call in enumerate(tool_calls):
                call_id = tool_call_value(tool_call, "call_id") or tool_call_value(
                    tool_call, "id"
                )
                try:
                    with active_tool_context(
                        root_frame,
                        tool_call,
                        function_item_ids.get(call_id),
                    ):
                        tool_message, new_extra_messages = call_tool(tool_call)
                except KeyboardInterrupt:
                    persist_tool_result_bundle(
                        interrupted_tool_outputs(tool_calls, index), messages
                    )
                    raise
                except Exception as exc:
                    persist_tool_result_bundle(
                        failed_tool_outputs(tool_calls, index, exc), messages
                    )
                    break
                persist_tool_result_bundle(
                    [tool_message, *new_extra_messages], messages
                )
                root_frame.messages = messages
                root_frame.active_item_ids = list(
                    session_runtime.get("active_item_ids") or []
                )
    finally:
        current_agent_frame.reset(frame_token)


def compact_configuration():
    return {
        "enabled": CONTEXT_COMPACTION_ENABLED,
        "context_window": CONTEXT_WINDOW,
        "threshold": CONTEXT_COMPACTION_THRESHOLD,
        "target_ratio": CONTEXT_COMPACTION_TARGET_RATIO,
        "compact_model": COMPACT_MODEL,
        "max_output_tokens": COMPACT_MAX_TOKENS,
        "reasoning_effort": COMPACT_REASONING_EFFORT,
    }


def configure_recursion_config(config=None):
    config = dict(config or {})
    resolved = {
        "max_depth": int(config.get("max_depth", RLM_MAX_DEPTH)),
        "max_calls": int(config.get("max_calls", RLM_MAX_CALLS)),
        "max_model_turns": int(
            config.get("max_model_turns", RLM_MAX_MODEL_TURNS)
        ),
    }
    invalid = [name for name, value in resolved.items() if value < 1]
    if invalid:
        raise ValueError(
            "Recursive limits must be positive integers: " + ", ".join(invalid)
        )
    session_runtime["recursion_config"] = resolved
    return copy.deepcopy(resolved)


def configure_model_client(provider, model, reasoning=None):
    global client, MODEL
    effort = REASONING_EFFORT
    if isinstance(reasoning, dict):
        effort = str(reasoning.get("effort") or "").strip()
    elif reasoning is not None:
        effort = str(reasoning).strip()
    client = llm_client.connect(provider, reasoning_effort=effort or None, trace=trace_tool)
    MODEL = model


def restore_compact_configuration(config):
    global CONTEXT_COMPACTION_ENABLED, CONTEXT_WINDOW
    global CONTEXT_COMPACTION_THRESHOLD, CONTEXT_COMPACTION_TARGET_RATIO
    global COMPACT_MODEL, COMPACT_MAX_TOKENS, COMPACT_REASONING_EFFORT
    config = config or {}
    CONTEXT_COMPACTION_ENABLED = bool(config.get("enabled", CONTEXT_COMPACTION_ENABLED))
    CONTEXT_WINDOW = int(config.get("context_window", CONTEXT_WINDOW))
    CONTEXT_COMPACTION_THRESHOLD = float(config.get("threshold", CONTEXT_COMPACTION_THRESHOLD))
    CONTEXT_COMPACTION_TARGET_RATIO = float(config.get("target_ratio", CONTEXT_COMPACTION_TARGET_RATIO))
    COMPACT_MODEL = config.get("compact_model", COMPACT_MODEL)
    COMPACT_MAX_TOKENS = int(config.get("max_output_tokens", COMPACT_MAX_TOKENS))
    COMPACT_REASONING_EFFORT = str(
        config.get("reasoning_effort", COMPACT_REASONING_EFFORT) or ""
    ).strip()


def controller_uid(controller):
    return str(getattr(controller, "uid", None) or controller.model.uid)


def session_scope_marker(session_id):
    return f"cryopilot-session:{session_id}"


def description_of(controller):
    return str(getattr(controller.model, "description", None) or "")


def unique_tagged_controller(controllers, marker, kind):
    matches = [controller for controller in controllers if description_of(controller) == marker]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple {kind} objects carry the session marker {marker!r}.")
    return matches[0] if matches else None


def ensure_session_scope(csw, store, session_id):
    session = store.get_session(session_id)
    manifest = session.manifest
    marker = session_scope_marker(session_id)

    if session.project_uid:
        try:
            csw.use_project(session.project_uid)
        except Exception as exc:
            raise RuntimeError(
                f"Saved CryoSPARC project {session.project_uid} is unavailable; no replacement was created."
            ) from exc
    elif manifest["dataset"]["kind"] == EXISTING_PROJECT:
        project_uid = manifest["project"]["project_uid"]
        try:
            csw.use_project(project_uid)
        except Exception as exc:
            raise RuntimeError(
                f"CryoSPARC project {project_uid} was not found, or this account cannot open it."
            ) from exc
        store.update_scope(session_id, project_uid=project_uid)
        session = store.get_session(session_id)
    else:
        project = unique_tagged_controller(csw.cs.find_projects(), marker, "project")
        if project is None:
            project = csw.create_project(
                manifest["project"]["title"],
                project_dir=manifest["project"]["parent_directory"],
                desc=marker,
            )
        project_uid = controller_uid(project)
        csw.use_project(project_uid)
        store.update_scope(session_id, project_uid=project_uid)
        session = store.get_session(session_id)

    if session.workspace_uid:
        try:
            csw.use_workspace(session.workspace_uid)
        except Exception as exc:
            raise RuntimeError(
                f"Saved CryoSPARC workspace {session.workspace_uid} is unavailable; no replacement was created."
            ) from exc
    else:
        workspace = unique_tagged_controller(csw.project.find_workspaces(), marker, "workspace")
        if workspace is None:
            workspace = csw.create_workspace(WORKSPACE_TITLE, desc=marker)
        workspace_uid = controller_uid(workspace)
        csw.use_workspace(workspace_uid)
        store.update_scope(session_id, workspace_uid=workspace_uid)

    session = store.get_session(session_id)
    bound_manifest = bind_manifest_scope(
        manifest,
        session_id=session_id,
        project_uid=session.project_uid,
        workspace_uid=session.workspace_uid,
    )
    store.update_manifest(session_id, bound_manifest)
    python_globals["csw"] = csw
    session_runtime["bound_csw"] = csw
    session_runtime["bound_scope"] = (
        str(session.project_uid),
        str(session.workspace_uid),
    )
    python_globals["session_context"] = copy.deepcopy(bound_manifest)
    return bound_manifest


def message_text(payload):
    """The text of an assistant message item."""
    if not isinstance(payload, dict) or payload.get("role") != "assistant":
        return ""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def earlier_session_notes(store, manifest, session_id, limit=2):
    """Notes from the newest earlier sessions in the project a session continues.

    Each is its last context summary and its last message, for the first
    prompt; see startup_cli.build_user_prompt.
    """

    if manifest["dataset"]["kind"] != EXISTING_PROJECT:
        return []
    notes = []
    for session in store.sessions_in_project(manifest["project"]["project_uid"]):
        if session.id == session_id:
            continue
        items = store.list_conversation_items(session.id)
        by_id = {item.id: item for item in items}
        compactions = store.list_compactions(session.id)
        summary = ""
        if compactions and compactions[-1].summary_item_id in by_id:
            payload = by_id[compactions[-1].summary_item_id].payload
            summary = str(payload.get("content") if isinstance(payload, dict) else payload or "")
            summary = summary.replace(COMPACTION_SUMMARY_PREFIX, "", 1).strip()
        last_message = next((text for text in (message_text(item.payload) for item in reversed(items)) if text.strip()), "")
        notes.append(
            {
                "id": session.id,
                "model": session.model,
                "status": session.status,
                "updated_at": session.updated_at,
                "last_error": session.last_error,
                "summary": summary,
                "last_message": last_message,
            }
        )
        if len(notes) == limit:
            break
    return notes


def append_resume_messages(messages):
    active = copy.deepcopy(agent_memory["active_messages"])
    active.extend(copy.deepcopy(messages))
    persist_appended_items(messages, active, "resume")


def set_awaiting_continuation(required):
    state = copy.deepcopy(session_runtime.get("durable_state") or {})
    state["awaiting_continuation"] = bool(required)
    session_runtime["durable_state"] = redact_persisted_value(state)
    python_globals["session_state"] = session_runtime["durable_state"]


def continuation_required(session):
    state = session_runtime.get("durable_state") or {}
    if "awaiting_continuation" not in state:
        set_awaiting_continuation(session.status == "completed")
    return bool(session_runtime["durable_state"].get("awaiting_continuation"))


def append_continuation_message(content):
    """Atomically append a continuation and clear its durable prompt requirement."""

    previous_state = copy.deepcopy(session_runtime.get("durable_state") or {})
    set_awaiting_continuation(False)
    message = {"role": "user", "content": content}
    active = copy.deepcopy(agent_memory["active_messages"])
    active.append(copy.deepcopy(message))
    try:
        item_ids = persist_appended_items([message], active, "resume")
    except BaseException:
        session_runtime["durable_state"] = previous_state
        python_globals["session_state"] = session_runtime["durable_state"]
        raise
    if item_ids:
        session_runtime["root_turn_item_id"] = item_ids[0]
        return item_ids[0]
    return None


def latest_root_turn_item_id():
    store, session_id = active_session_store()
    if store is None:
        return None
    for record in reversed(store.list_conversation_items(session_id)):
        if record.origin not in {"bootstrap", "resume"}:
            continue
        payload = record.payload
        if isinstance(payload, dict) and payload.get("role") == "user":
            return record.id
    return None


def build_resume_note(session, durable_state, recovered_recursive_calls=()):
    known_jobs = durable_state.get("known_jobs") if isinstance(durable_state, dict) else None
    jobs_text = json.dumps(known_jobs or [], ensure_ascii=False, sort_keys=True)
    recovered_ids = [int(call.id) for call in recovered_recursive_calls]
    recursive_recovery_text = ""
    if recovered_ids:
        recursive_recovery_text = (
            "\nIncomplete recursive branches from the stopped process were marked "
            f"interrupted and were not replayed: {recovered_ids}. Inspect their exact "
            "traces with rlm.history(call_id) before deciding whether to delegate again.\n"
        )
    return {
        "role": "developer",
        "content": (
            "[SESSION RESUME]\n"
            f"Resuming session {session.id} in project {session.project_uid}, workspace {session.workspace_uid}. "
            "The exact SQLite transcript and current compacted context were restored. Arbitrary Python variables "
            "were not serialized; reacquire job controllers by UID with csw.find_job(...). CryoSPARC jobs may "
            "have continued while this process was stopped, so inspect current status before repeating any side effect.\n"
            f"Last known workspace jobs: {jobs_text}\n\n"
            f"{recursive_recovery_text}"
            f"{RLM_INSTRUCTIONS}"
        ),
    }


def recover_interrupted_recursive_calls(store, session_id):
    """Close crash-left child traces only after CryoSPARC scope is rebound."""

    recovered = store.recover_incomplete_recursive_calls(
        session_id,
        error={
            "type": "process_interrupted",
            "message": (
                "The prior harness process stopped before this recursive branch "
                "finished. It was not resumed or replayed."
            ),
        },
    )
    if recovered:
        trace_tool(
            "[rlm-recovery] marked interrupted without replay: "
            + ", ".join(str(call.id) for call in recovered)
        )
    return recovered


def print_session_resume(session_id, *, store=None, saved=True):
    label = "Session saved" if saved else "Session ID"
    print(f"{label}: {session_id}")
    command = "python reconstruction_agent/harness.py"
    if store is not None:
        database_path = store.path
        if str(database_path) != ":memory:":
            database_path = Path(database_path).resolve()
        command += f" --session-db {shlex.quote(str(database_path))}"
    command += f" --resume {shlex.quote(str(session_id))}"
    print(f"Resume with: {command}")


def reconcile_committed_checkpoint():
    """Make SQLite authoritative across the tiny post-commit signal window.

    Transcript append/checkpoint transactions commit before their in-process
    mirrors can be updated. If a signal lands between those operations, restore
    the newer committed snapshot so shutdown cannot supersede it with stale
    active IDs or history.
    """

    store, session_id = active_session_store()
    if store is None:
        return
    snapshot = store.load_snapshot(session_id)
    if snapshot.checkpoint is None:
        return
    committed_ids = list(snapshot.checkpoint.active_item_ids)
    if (
        committed_ids != list(session_runtime.get("active_item_ids") or [])
        or snapshot.active_messages != agent_memory["active_messages"]
        or snapshot.full_history != agent_memory["full_history"]
        or snapshot.checkpoint.last_summary_item_id
        != session_runtime.get("last_summary_item_id")
    ):
        restore_session_memory(snapshot)


def finish_session(store, session_id, status, error=None):
    safe_error = None if error is None else redact_runtime_secrets(truncate_middle(str(error), 2000))
    try:
        reconcile_committed_checkpoint()
        if status == "completed":
            set_awaiting_continuation(True)
        persist_checkpoint(refresh_jobs=True)
    except BaseException as checkpoint_error:
        checkpoint_message = redact_runtime_secrets(
            f"Checkpoint failed during shutdown: {truncate_middle(str(checkpoint_error), 1200)}"
        )
        safe_error = (
            checkpoint_message
            if safe_error is None
            else f"{safe_error}; {checkpoint_message}"
        )
        if status == "completed":
            status = "error"
    store.set_status(session_id, status, last_error=safe_error)
    print_session_resume(session_id, store=store, saved=True)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Interactive single-project CryoSPARC reconstruction agent."
    )
    parser.add_argument("--resume", metavar="SESSION_ID", help="Resume a persisted session by ID.")
    parser.add_argument(
        "--from-project",
        metavar="PROJECT_UID",
        help=(
            "Start a new session that continues an existing CryoSPARC project (for example P12), "
            "in a new workspace of its own, instead of importing a dataset."
        ),
    )
    parser.add_argument(
        "--session-db",
        default=os.environ.get("CRYOPILOT_SESSION_DB"),
        help="SQLite session database path (default: ~/.cryopilot/sessions.sqlite3).",
    )
    parser.add_argument("--no-tool-trace", action="store_true", help="Do not print model tool calls to stderr.")
    parser.add_argument("--ollama", action="store_true", help=f"Use local Ollama at {OLLAMA_BASE_URL} with {OLLAMA_MODEL}.")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Override the model for a new session. A Claude model such as "
            "claude-sonnet-5-5 runs on the Anthropic API (ANTHROPIC_API_KEY)."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        choices=("none", "minimal", "low", "medium", "high", "xhigh"),
        help="Reasoning effort for a new session (Claude's effort level for Claude models).",
    )
    parser.add_argument("--cryosparc-url", default=None)
    parser.add_argument(
        "--processing-guide",
        default=None,
        help=(
            "Processing guide to load: 'original', 'experimental', or a path to a .md file "
            f"(default: $CRYOEM_GUIDE or {DEFAULT_CRYOEM_GUIDE!r})."
        ),
    )
    parser.add_argument("--cryosparc-email", default=None)
    parser.add_argument(
        "--rlm-max-depth",
        type=int,
        default=None,
        help=f"Maximum recursive child depth for a new session (default: {RLM_MAX_DEPTH}).",
    )
    parser.add_argument(
        "--rlm-max-calls",
        type=int,
        default=None,
        help=f"Maximum child invocations per root user turn (default: {RLM_MAX_CALLS}).",
    )
    parser.add_argument(
        "--rlm-max-model-turns",
        type=int,
        default=None,
        help=(
            "Maximum child model requests per root user turn, including child "
            f"compaction (default: {RLM_MAX_MODEL_TURNS})."
        ),
    )
    return parser


def main(argv=None):
    global TRACE_TOOLS
    global CRYOEM_GUIDE_PATH
    args = build_parser().parse_args(argv)
    if args.no_tool_trace:
        TRACE_TOOLS = False
    if args.processing_guide:
        CRYOEM_GUIDE_PATH = resolve_cryoem_guide(args.processing_guide)
        reload_cryoem_data_processing()

    store = SessionStore(args.session_db)
    session_id = None
    try:
        if args.resume:
            if (
                args.from_project
                or args.ollama
                or args.model
                or args.reasoning_effort
                or args.cryosparc_url
                or args.cryosparc_email
                or args.rlm_max_depth is not None
                or args.rlm_max_calls is not None
                or args.rlm_max_model_turns is not None
            ):
                raise ValueError(
                    "--resume restores the project, provider, model, reasoning, recursion "
                    "limits, and CryoSPARC connection settings; do not override them."
                )
            snapshot = store.load_snapshot(args.resume)
            session = snapshot.session
            session_id = session.id
            configure_session_runtime(store, session_id)
            restore_session_memory(snapshot)
            continuation_required(session)
            restore_compact_configuration(session.compact_config)
            configure_recursion_config(session.recursion_config)
            configure_model_client(session.provider, session.model, session.reasoning)
            credentials = collect_cryosparc_credentials(
                url=session.cryosparc_url,
                email=session.cryosparc_email,
                password=os.environ.get("CRYOSPARC_PASSWORD"),
            )
            register_sensitive_value(credentials["password"])
            csw = connect_cryosparc(**{
                "base_url": credentials["url"],
                "email": credentials["email"],
                "password": credentials["password"],
            })
            manifest = ensure_session_scope(csw, store, session_id)
            recovered_recursive_calls = recover_interrupted_recursive_calls(
                store, session_id
            )
            snapshot = store.load_snapshot(session_id)
            python_globals["session_context"] = copy.deepcopy(manifest)

            if snapshot.active_messages:
                snapshot_workspace_jobs()
                pending_calls = unmatched_function_calls(agent_memory["active_messages"])
                if pending_calls:
                    append_resume_messages(interrupted_tool_outputs(pending_calls))
                append_resume_messages(
                    [
                        build_resume_note(
                            snapshot.session,
                            session_runtime["durable_state"],
                            recovered_recursive_calls,
                        )
                    ]
                )
                if continuation_required(snapshot.session):
                    continuation = prompt_required("Continuation request: ")
                    store.set_status(session_id, "running")
                    append_continuation_message(continuation)
                else:
                    store.set_status(session_id, "running")
                run_agent(resume=True)
            else:
                developer = f"{INSTRUCTIONS}\n\n{build_developer_instruction(manifest)}"
                prompt = build_user_prompt(manifest, earlier_session_notes(store, manifest, session_id))
                store.set_status(session_id, "running")
                run_agent(prompt, developer_instructions=developer)
        else:
            model = args.model or (OLLAMA_MODEL if args.ollama else MODEL)
            provider = "ollama" if args.ollama else llm_client.provider_for_model(model)
            reasoning_effort = None if args.ollama else (args.reasoning_effort or REASONING_EFFORT)
            recursion_config = configure_recursion_config(
                {
                    "max_depth": (
                        RLM_MAX_DEPTH
                        if args.rlm_max_depth is None
                        else args.rlm_max_depth
                    ),
                    "max_calls": (
                        RLM_MAX_CALLS
                        if args.rlm_max_calls is None
                        else args.rlm_max_calls
                    ),
                    "max_model_turns": (
                        RLM_MAX_MODEL_TURNS
                        if args.rlm_max_model_turns is None
                        else args.rlm_max_model_turns
                    ),
                }
            )
            configure_model_client(
                provider,
                model,
                None if reasoning_effort is None else {"effort": reasoning_effort},
            )
            credentials = collect_cryosparc_credentials(
                url=args.cryosparc_url or os.environ.get("CRYOSPARC_BASE_URL"),
                email=args.cryosparc_email or os.environ.get("CRYOSPARC_EMAIL"),
                password=os.environ.get("CRYOSPARC_PASSWORD"),
            )
            register_sensitive_value(credentials["password"])
            csw = connect_cryosparc(
                base_url=credentials["url"],
                email=credentials["email"],
                password=credentials["password"],
            )
            print("Connected to CryoSPARC.")
            if args.from_project:
                project_uid = args.from_project.strip().upper()
                try:
                    project = csw.cs.find_project(project_uid)
                except Exception as exc:
                    raise ValueError(
                        f"CryoSPARC project {project_uid} was not found, or this account cannot open it."
                    ) from exc
                title = str(getattr(project.model, "title", "") or project_uid)
                print(f"Continuing project {project_uid} ({title}) in a new workspace.")
                manifest = collect_continuation_manifest(project_uid, title)
            else:
                manifest = collect_session_manifest()
            session_id = store.create_session(
                provider=provider,
                model=model,
                reasoning=None if reasoning_effort is None else {"effort": reasoning_effort},
                compact_config=compact_configuration(),
                recursion_config=recursion_config,
                cryosparc_url=credentials["url"],
                cryosparc_email=credentials["email"],
                manifest=manifest,
                status="initializing",
            )
            configure_session_runtime(store, session_id)
            configure_recursion_config(recursion_config)
            register_sensitive_value(credentials["password"])
            print_session_resume(session_id, store=store, saved=False)
            manifest = ensure_session_scope(csw, store, session_id)
            developer = f"{INSTRUCTIONS}\n\n{build_developer_instruction(manifest)}"
            prompt = build_user_prompt(manifest, earlier_session_notes(store, manifest, session_id))
            store.set_status(session_id, "running")
            run_agent(prompt, developer_instructions=developer)

        finish_session(store, session_id, "completed")
        return 0
    except KeyboardInterrupt:
        if session_id is not None:
            finish_session(store, session_id, "interrupted", error="Ctrl+C")
        return 130
    except SessionNotFoundError:
        raise
    except Exception as exc:
        if session_id is not None:
            try:
                finish_session(store, session_id, "error", error=exc)
            except Exception:
                pass
        raise
    finally:
        configure_session_runtime()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
