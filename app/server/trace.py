"""Read-only view over the harness session database for the web UI.

Turns the exact persisted Responses-API items into compact, display-oriented
trace nodes. Large payloads (tool output, embedded images) are truncated or
replaced with references that the UI fetches on demand.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

COMPACTION_PREFIX = "[CONTEXT COMPACTION - REFERENCE ONLY]"
TEXT_LIMIT = 6_000
CODE_LIMIT = 20_000


class TraceError(RuntimeError):
    pass


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _clip(text: str, limit: int = TEXT_LIMIT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in {"input_text", "output_text", "text"}:
                    parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _image_refs(content: Any) -> list[dict[str, Any]]:
    refs = []
    if isinstance(content, list):
        for index, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "input_image":
                url = str(part.get("image_url") or "")
                mime = "image/png"
                if url.startswith("data:"):
                    mime = url[5:].split(";", 1)[0] or mime
                refs.append({"index": index, "mime": mime})
    return refs


def classify(payload: Any, origin: str) -> dict[str, Any]:
    """Return a display node for one persisted conversation item."""

    node: dict[str, Any] = {"kind": "unknown", "title": "Item"}
    if not isinstance(payload, dict):
        text, truncated = _clip(json.dumps(payload, ensure_ascii=False))
        node.update({"text": text, "truncated": truncated})
        return node

    item_type = payload.get("type")
    role = payload.get("role")

    if item_type == "function_call":
        name = payload.get("name") or "tool"
        arguments = payload.get("arguments")
        args = _loads(arguments) if isinstance(arguments, str) else arguments
        node.update({"kind": "tool_call", "title": name, "name": name, "call_id": payload.get("call_id") or payload.get("id")})
        if isinstance(args, dict):
            code = args.get("code")
            other = {key: value for key, value in args.items() if key != "code"}
            if isinstance(code, str):
                clipped, truncated = _clip(code, CODE_LIMIT)
                node["code"] = clipped
                node["truncated"] = truncated
            node["arguments"] = other
        else:
            text, truncated = _clip(str(arguments))
            node["text"] = text
            node["truncated"] = truncated
        return node

    if item_type == "function_call_output":
        output = payload.get("output")
        parsed = _loads(output) if isinstance(output, str) else output
        node.update({"kind": "tool_result", "title": "Result", "call_id": payload.get("call_id")})
        if isinstance(parsed, dict):
            node["ok"] = parsed.get("ok")
            stdout, out_trunc = _clip(str(parsed.get("stdout") or ""))
            stderr, err_trunc = _clip(str(parsed.get("stderr") or ""), 3_000)
            tb, tb_trunc = _clip(str(parsed.get("traceback") or ""), 4_000)
            node["stdout"] = stdout
            node["stderr"] = stderr
            node["traceback"] = tb
            node["error"] = parsed.get("error")
            node["truncated"] = out_trunc or err_trunc or tb_trunc
            extra = {
                key: value
                for key, value in parsed.items()
                if key not in {"ok", "stdout", "stderr", "traceback", "error"}
            }
            if extra:
                text, truncated = _clip(json.dumps(extra, ensure_ascii=False, indent=2), 2_000)
                node["extra"] = text
                node["truncated"] = node["truncated"] or truncated
        else:
            text, truncated = _clip(str(output))
            node["text"] = text
            node["truncated"] = truncated
        return node

    if item_type == "reasoning":
        summary = payload.get("summary") or []
        texts = []
        for entry in summary:
            if isinstance(entry, dict):
                texts.append(str(entry.get("text") or ""))
            elif isinstance(entry, str):
                texts.append(entry)
        text, truncated = _clip("\n\n".join(part for part in texts if part))
        node.update(
            {
                "kind": "reasoning",
                "title": "Reasoning",
                "text": text,
                "truncated": truncated,
                "encrypted": bool(payload.get("encrypted_content")),
            }
        )
        return node

    if item_type == "message" or role in {"assistant", "user", "developer", "system"}:
        content = payload.get("content")
        text = _content_text(content)
        images = _image_refs(content)
        if role == "assistant":
            clipped, truncated = _clip(text, 20_000)
            node.update({"kind": "assistant", "title": "Agent", "text": clipped, "truncated": truncated})
            return node
        if role == "user":
            if images:
                clipped, truncated = _clip(text, 2_000)
                node.update({"kind": "image", "title": "Image loaded", "text": clipped, "truncated": truncated, "images": images})
                return node
            clipped, truncated = _clip(text, 20_000)
            node.update({"kind": "user", "title": "You", "text": clipped, "truncated": truncated, "bootstrap": origin == "bootstrap"})
            return node
        if isinstance(text, str) and text.startswith(COMPACTION_PREFIX):
            clipped, truncated = _clip(text, 12_000)
            node.update({"kind": "compaction", "title": "Context compacted", "text": clipped, "truncated": truncated})
            return node
        if isinstance(text, str) and text.startswith("[SESSION RESUME]"):
            clipped, truncated = _clip(text, 4_000)
            node.update({"kind": "resume", "title": "Session resumed", "text": clipped, "truncated": truncated})
            return node
        clipped, truncated = _clip(text, 20_000)
        node.update({"kind": "developer", "title": "Instructions", "text": clipped, "truncated": truncated})
        return node

    text, truncated = _clip(json.dumps(payload, ensure_ascii=False, indent=2))
    node.update({"kind": item_type or "unknown", "title": str(item_type or "Item"), "text": text, "truncated": truncated})
    return node


class HarnessTraceReader:
    """Opens the harness database read-only. Never writes or migrates it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise TraceError(f"Session database not found: {self.path}")
        uri = f"file:{self.path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise TraceError(f"Harness session {session_id} not found")
            counts = connection.execute(
                "SELECT COUNT(*) AS n, MAX(id) AS last_id FROM conversation_items WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            recursive = connection.execute(
                "SELECT COUNT(*) AS n FROM recursive_calls WHERE session_id = ?", (session_id,)
            ).fetchone()
        return {
            "id": row["id"],
            "status": row["status"],
            "provider": row["provider"],
            "model": row["model"],
            "reasoning": _loads(row["reasoning_json"]),
            "recursion_config": _loads(row["recursion_config_json"]),
            "cryosparc_url": row["cryosparc_url"],
            "cryosparc_email": row["cryosparc_email"],
            "project_uid": row["project_uid"],
            "workspace_uid": row["workspace_uid"],
            "manifest": _loads(row["manifest_json"]),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "item_count": counts["n"],
            "last_item_id": counts["last_id"],
            "recursive_call_count": recursive["n"],
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, status, provider, model, project_uid, workspace_uid, manifest_json, created_at, updated_at FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        sessions = []
        for row in rows:
            manifest = _loads(row["manifest_json"]) or {}
            sessions.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "provider": row["provider"],
                    "model": row["model"],
                    "project_uid": row["project_uid"],
                    "workspace_uid": row["workspace_uid"],
                    "title": (manifest.get("project") or {}).get("title"),
                    "user_goal": manifest.get("user_goal"),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return sessions

    def items(self, session_id: str, after: int = 0, limit: int = 5000) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, sequence, origin, payload_json, created_at FROM conversation_items "
                "WHERE session_id = ? AND id > ? ORDER BY sequence LIMIT ?",
                (session_id, after, limit),
            ).fetchall()
        nodes = []
        for row in rows:
            payload = _loads(row["payload_json"])
            node = classify(payload, row["origin"])
            node.update(
                {
                    "id": row["id"],
                    "sequence": row["sequence"],
                    "origin": row["origin"],
                    "created_at": row["created_at"],
                }
            )
            nodes.append(node)
        return nodes

    def item_payload(self, session_id: str, item_id: int) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM conversation_items WHERE session_id = ? AND id = ?",
                (session_id, item_id),
            ).fetchone()
        if row is None:
            raise TraceError(f"Item {item_id} not found")
        return _loads(row["payload_json"])

    def item_image(self, session_id: str, item_id: int, index: int) -> tuple[str, bytes]:
        return self._image(self.item_payload(session_id, item_id), index)

    def recursive_item_image(self, session_id: str, call_id: int, item_id: int, index: int) -> tuple[str, bytes]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recursive_items WHERE session_id = ? AND call_id = ? AND id = ?",
                (session_id, call_id, item_id),
            ).fetchone()
        if row is None:
            raise TraceError(f"Item {item_id} not found")
        return self._image(_loads(row["payload_json"]), index)

    @staticmethod
    def _image(payload: Any, index: int) -> tuple[str, bytes]:
        import base64

        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, list) or index >= len(content):
            raise TraceError("Image not found")
        part = content[index]
        url = str(part.get("image_url") or "") if isinstance(part, dict) else ""
        if not url.startswith("data:"):
            raise TraceError("Image is not embedded")
        header, _, data = url.partition(",")
        mime = header[5:].split(";", 1)[0] or "image/png"
        return mime, base64.b64decode(data)

    def recursive_calls(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recursive_calls WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        calls = []
        for row in rows:
            final_output = _loads(row["final_output_json"])
            calls.append(
                {
                    "id": row["id"],
                    "parent_call_id": row["parent_call_id"],
                    "root_turn_item_id": row["root_turn_item_id"],
                    "root_function_call_item_id": row["root_function_call_item_id"],
                    "root_function_call_id": row["root_function_call_id"],
                    "invoking_call_id": row["invoking_call_id"],
                    "depth": row["depth"],
                    "ordinal": row["ordinal"],
                    "status": row["status"],
                    "metrics": _loads(row["metrics_json"]),
                    "error": _loads(row["error_json"]),
                    "final_output": final_output if isinstance(final_output, str) else json.dumps(final_output) if final_output is not None else None,
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                }
            )
        return calls

    def recursive_items(self, session_id: str, call_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, sequence, origin, payload_json, created_at FROM recursive_items "
                "WHERE session_id = ? AND call_id = ? ORDER BY sequence",
                (session_id, call_id),
            ).fetchall()
        nodes = []
        for row in rows:
            node = classify(_loads(row["payload_json"]), row["origin"])
            node.update(
                {
                    "id": row["id"],
                    "sequence": row["sequence"],
                    "origin": row["origin"],
                    "created_at": row["created_at"],
                    "recursive": True,
                }
            )
            nodes.append(node)
        return nodes

    def last_assistant_text(self, session_id: str) -> str:
        """The agent's newest message in the main conversation, or an empty string."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM conversation_items WHERE session_id = ? AND origin = 'model' "
                "ORDER BY sequence DESC LIMIT 50",
                (session_id,),
            )
            for row in rows:
                payload = _loads(row["payload_json"])
                if isinstance(payload, dict) and payload.get("role") == "assistant":
                    text = _content_text(payload.get("content")).strip()
                    if text:
                        return text
        return ""

    def raw_items(self, session_id: str):
        """Every item of the main conversation, whole, in order: (origin, payload, created_at)."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT origin, payload_json, created_at FROM conversation_items WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            )
            for row in rows:
                yield row["origin"], _loads(row["payload_json"]), row["created_at"]

    def raw_recursive_items(self, session_id: str, call_id: int):
        """Every item of one sub-agent's conversation, whole, in order."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT origin, payload_json, created_at FROM recursive_items "
                "WHERE session_id = ? AND call_id = ? ORDER BY sequence",
                (session_id, call_id),
            )
            for row in rows:
                yield row["origin"], _loads(row["payload_json"]), row["created_at"]

    def known_jobs(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT durable_state_json FROM checkpoints WHERE session_id = ? ORDER BY ordinal DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return []
        state = _loads(row["durable_state_json"]) or {}
        jobs = state.get("known_jobs") if isinstance(state, dict) else None
        return jobs if isinstance(jobs, list) else []
