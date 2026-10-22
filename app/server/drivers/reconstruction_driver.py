#!/usr/bin/env python3
"""Long-lived driver that runs the CryoSPARC reconstruction harness in-process.

It mirrors ``harness.main`` but replaces every terminal prompt with a JSON
manifest supplied by the web app, and keeps the process (and therefore the
persistent Python REPL and the authenticated CryoSPARC connection) alive
between chat turns. The transcript is persisted by the harness itself in the
shared session database; the app reads the trace from there.

Usage:
    reconstruction_driver.py --session-db PATH --new MANIFEST.json --config CONFIG.json
    reconstruction_driver.py --session-db PATH --resume SESSION_ID

Credentials come from the environment (CRYOSPARC_BASE_URL, CRYOSPARC_EMAIL,
CRYOSPARC_PASSWORD, OPENAI_API_KEY) and are never written to the manifest.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import sys
import traceback
from pathlib import Path

DRIVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = DRIVER_DIR.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DRIVER_DIR))

from protocol import CommandReader, Emitter, redirect_stdout_to_stderr  # noqa: E402

emitter = Emitter()
redirect_stdout_to_stderr()

import harness as H  # noqa: E402
from cryosparc_repl import connect_cryosparc  # noqa: E402
from session_store import SessionStore  # noqa: E402
from startup_cli import (  # noqa: E402
    build_developer_instruction,
    build_user_prompt,
    collect_cryosparc_credentials,
)


SCOPE_BOUND = {"value": False}


def status(state: str, detail: str = "") -> None:
    emitter.emit("status", state=state, detail=detail)


def log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-db", required=True)
    parser.add_argument("--new", help="Path to a JSON manifest for a new session.")
    parser.add_argument("--config", help="Path to a JSON agent config for a new session.")
    parser.add_argument("--resume", help="Harness session ID to resume.")
    return parser.parse_args()


def connect(url: str | None, email: str | None):
    credentials = collect_cryosparc_credentials(
        url=url or os.environ.get("CRYOSPARC_BASE_URL"),
        email=email or os.environ.get("CRYOSPARC_EMAIL"),
        password=os.environ.get("CRYOSPARC_PASSWORD"),
        input_fn=lambda prompt: (_ for _ in ()).throw(RuntimeError(f"Missing CryoSPARC setting: {prompt.strip()}")),
        getpass_fn=lambda prompt: (_ for _ in ()).throw(RuntimeError("CRYOSPARC_PASSWORD is not set")),
    )
    H.register_sensitive_value(credentials["password"])
    status("connecting", f"Connecting to CryoSPARC at {credentials['url']}")
    csw = connect_cryosparc(
        base_url=credentials["url"],
        email=credentials["email"],
        password=credentials["password"],
    )
    log("Connected to CryoSPARC.")
    return csw, credentials


def settle_after_turn(store: SessionStore, session_id: str, state: str, error: str | None = None) -> None:
    """Persist the checkpoint after a turn without closing the session."""

    safe_error = None if error is None else H.redact_runtime_secrets(H.truncate_middle(str(error), 2000))
    try:
        H.reconcile_committed_checkpoint()
        if state == "completed":
            H.set_awaiting_continuation(True)
        H.persist_checkpoint(refresh_jobs=True)
    except BaseException as checkpoint_error:  # pragma: no cover - defensive
        message = H.redact_runtime_secrets(
            f"Checkpoint failed: {H.truncate_middle(str(checkpoint_error), 1200)}"
        )
        safe_error = message if safe_error is None else f"{safe_error}; {message}"
        if state == "completed":
            state = "error"
    store.set_status(session_id, state, last_error=safe_error)


def run_turn(store: SessionStore, session_id: str, *, prompt=None, developer=None, resume=False) -> None:
    status("running", "Agent is working")
    store.set_status(session_id, "running")
    try:
        H.run_agent(prompt, developer_instructions=developer, resume=resume)
    except KeyboardInterrupt:
        settle_after_turn(store, session_id, "interrupted", error="Stopped from the web app")
        status("interrupted", "Stopped")
        emitter.emit("turn_end", outcome="interrupted")
        return
    except Exception as exc:
        log(traceback.format_exc())
        settle_after_turn(store, session_id, "error", error=exc)
        status("error", H.redact_runtime_secrets(H.truncate_middle(str(exc), 800)))
        emitter.emit("turn_end", outcome="error")
        return
    settle_after_turn(store, session_id, "completed")
    status("idle", "Waiting for your next message")
    emitter.emit("turn_end", outcome="completed")


def command_loop(store: SessionStore, session_id: str, reader: CommandReader, *, initial_continuation_required: bool) -> None:
    continuation_required = initial_continuation_required
    while True:
        try:
            command = reader.next()
        except KeyboardInterrupt:
            # A stop request that arrived while idle; nothing to interrupt.
            continue
        if command is None or command.get("op") == "shutdown":
            log("Driver shutting down.")
            return
        if command.get("op") != "message":
            continue
        content = str(command.get("content") or "").strip()
        if not content:
            continue
        emitter.emit("user_turn_accepted")
        H.append_continuation_message(content)
        continuation_required = False
        run_turn(store, session_id, resume=True)


def start_new(args: argparse.Namespace, store: SessionStore, reader: CommandReader) -> str:
    manifest = json.loads(Path(args.new).read_text(encoding="utf-8"))
    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}

    provider = config.get("provider") or "openai"
    model = config.get("model") or (H.OLLAMA_MODEL if provider == "ollama" else H.MODEL)
    reasoning_effort = None if provider == "ollama" else (config.get("reasoning_effort") or H.REASONING_EFFORT)
    recursion_config = H.configure_recursion_config(
        {
            "max_depth": config.get("rlm_max_depth", H.RLM_MAX_DEPTH),
            "max_calls": config.get("rlm_max_calls", H.RLM_MAX_CALLS),
            "max_model_turns": config.get("rlm_max_model_turns", H.RLM_MAX_MODEL_TURNS),
        }
    )
    H.configure_model_client(
        provider,
        model,
        None if reasoning_effort is None else {"effort": reasoning_effort},
    )
    csw, credentials = connect(config.get("cryosparc_url"), config.get("cryosparc_email"))

    session_id = store.create_session(
        provider=provider,
        model=model,
        reasoning=None if reasoning_effort is None else {"effort": reasoning_effort},
        compact_config=H.compact_configuration(),
        recursion_config=recursion_config,
        cryosparc_url=credentials["url"],
        cryosparc_email=credentials["email"],
        manifest=manifest,
        status="initializing",
    )
    emitter.emit("session", harness_session_id=session_id)
    H.configure_session_runtime(store, session_id)
    H.configure_recursion_config(recursion_config)
    H.register_sensitive_value(credentials["password"])
    status("initializing", "Creating the CryoSPARC project and workspace")
    bound_manifest = H.ensure_session_scope(csw, store, session_id)
    SCOPE_BOUND["value"] = True
    emitter.emit(
        "scope",
        project_uid=bound_manifest["project"]["project_uid"],
        workspace_uid=bound_manifest["project"]["workspace_uid"],
    )
    developer = f"{H.INSTRUCTIONS}\n\n{build_developer_instruction(bound_manifest)}"
    prompt = build_user_prompt(bound_manifest)
    run_turn(store, session_id, prompt=prompt, developer=developer)
    command_loop(store, session_id, reader, initial_continuation_required=True)
    return session_id


def start_resume(args: argparse.Namespace, store: SessionStore, reader: CommandReader) -> str:
    snapshot = store.load_snapshot(args.resume)
    session = snapshot.session
    session_id = session.id
    emitter.emit("session", harness_session_id=session_id)
    H.configure_session_runtime(store, session_id)
    H.restore_session_memory(snapshot)
    H.continuation_required(session)
    H.restore_compact_configuration(session.compact_config)
    H.configure_recursion_config(session.recursion_config)
    H.configure_model_client(session.provider, session.model, session.reasoning)
    csw, _credentials = connect(session.cryosparc_url, session.cryosparc_email)
    status("initializing", "Re-binding the CryoSPARC project and workspace")
    manifest = H.ensure_session_scope(csw, store, session_id)
    SCOPE_BOUND["value"] = True
    emitter.emit(
        "scope",
        project_uid=manifest["project"]["project_uid"],
        workspace_uid=manifest["project"]["workspace_uid"],
    )
    recovered = H.recover_interrupted_recursive_calls(store, session_id)
    snapshot = store.load_snapshot(session_id)
    H.python_globals["session_context"] = copy.deepcopy(manifest)

    if snapshot.active_messages:
        H.snapshot_workspace_jobs()
        pending_calls = H.unmatched_function_calls(H.agent_memory["active_messages"])
        if pending_calls:
            H.append_resume_messages(H.interrupted_tool_outputs(pending_calls))
        H.append_resume_messages(
            [H.build_resume_note(snapshot.session, H.session_runtime["durable_state"], recovered)]
        )
        if H.continuation_required(snapshot.session):
            status("idle", "Session restored. Send a message to continue.")
            command_loop(store, session_id, reader, initial_continuation_required=True)
        else:
            store.set_status(session_id, "running")
            run_turn(store, session_id, resume=True)
            command_loop(store, session_id, reader, initial_continuation_required=True)
    else:
        developer = f"{H.INSTRUCTIONS}\n\n{build_developer_instruction(manifest)}"
        prompt = build_user_prompt(manifest)
        run_turn(store, session_id, prompt=prompt, developer=developer)
        command_loop(store, session_id, reader, initial_continuation_required=True)
    return session_id


def main() -> int:
    args = parse_args()
    if not args.new and not args.resume:
        status("error", "Either --new or --resume is required")
        return 2
    signal.signal(signal.SIGINT, signal.default_int_handler)
    reader = CommandReader()
    store = SessionStore(args.session_db)
    session_id = None
    try:
        if args.resume:
            session_id = start_resume(args, store, reader)
        else:
            session_id = start_new(args, store, reader)
        status("stopped", "Driver exited")
        return 0
    except KeyboardInterrupt:
        status("stopped", "Driver interrupted")
        return 130
    except Exception as exc:
        log(traceback.format_exc())
        message = H.redact_runtime_secrets(H.truncate_middle(str(exc), 1200))
        if session_id is not None and SCOPE_BOUND["value"]:
            try:
                store.set_status(session_id, "error", last_error=message)
            except Exception:
                pass
        status("error", message)
        emitter.emit("error", message=message)
        return 1
    finally:
        H.configure_session_runtime()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
