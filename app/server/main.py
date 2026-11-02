"""FastAPI application for the CryoPilot web app.

Shared state lives in ``state.py``. This module holds projects, sessions,
and transcripts; the other routes are in ``routes_*.py``.
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import attachments, auth, files as file_roots, guides, modeling_toolchain
from . import routes_cryosparc, routes_daq, routes_files, routes_setup, routes_terminal
from .config import FRONTEND_DIST
from .manifests import ManifestError, build_agent_config, build_manifest, compose_modeling_request, provider_for_model
from .state import (
    RECONSTRUCTION_SUPPORTED,
    REASONING_EFFORTS,
    api_error,
    bus,
    cryosparc_settings,
    defaults,
    driver_env,
    processes,
    processing_guide,
    require_model_key,
    require_project,
    require_session,
    session_runtime_view,
    shutting_down,
    store,
    trace_reader,
)
from .store import AppStoreError
from .trace import TraceError

app = FastAPI(title="CryoPilot", docs_url=None, redoc_url=None)
app.add_middleware(auth.AccessTokenMiddleware)
app.include_router(auth.router)
app.include_router(routes_setup.router)
app.include_router(routes_cryosparc.router)
app.include_router(routes_files.router)
app.include_router(routes_daq.router)
app.include_router(routes_terminal.router)

modeling_toolchain.status()  # start the first check in the background

ALLOW_RECONSTRUCTION = RECONSTRUCTION_SUPPORTED


@app.on_event("shutdown")
def _shutdown() -> None:
    processes.stop_all()
    routes_terminal.shutdown()
    store.close()


# ---------------------------------------------------------------- projects
@app.get("/api/projects")
def list_projects() -> dict[str, Any]:
    return {"projects": store.list_projects()}


@app.post("/api/projects")
async def create_project(request: Request) -> dict[str, Any]:
    body = await request.json()
    try:
        project = store.create_project(
            title=str(body.get("title") or ""),
            description=str(body.get("description") or ""),
            parent_directory=str(body.get("parent_directory") or ""),
            settings=body.get("settings") or {},
        )
    except AppStoreError as exc:
        raise api_error(400, str(exc))
    project["directories"] = file_roots.ensure_project_dirs(project["slug"])
    project["sessions"] = []
    return project


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    project = require_project(project_id)
    project["directories"] = file_roots.ensure_project_dirs(project["slug"])
    project["sessions"] = [session_runtime_view(s) for s in store.list_sessions(project["id"])]
    return project


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: str, request: Request) -> dict[str, Any]:
    project = require_project(project_id)
    body = await request.json()
    fields = {key: body.get(key) for key in ("title", "description", "parent_directory", "settings") if key in body}
    return store.update_project(project["id"], **fields)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict[str, Any]:
    project = require_project(project_id)
    for session in store.list_sessions(project["id"]):
        processes.stop(session["id"])
    store.delete_project(project["id"])
    return {"ok": True}


# ---------------------------------------------------------------- sessions
@app.get("/api/projects/{project_id}/sessions")
def list_sessions(project_id: str, kind: str | None = None) -> dict[str, Any]:
    project = require_project(project_id)
    return {"sessions": [session_runtime_view(s) for s in store.list_sessions(project["id"], kind)]}


def _reconstruction_agent_config(requested: dict[str, Any]) -> dict[str, Any]:
    saved = defaults()
    merged = {"model": saved["reconstruction_model"], "reasoning_effort": saved["reconstruction_effort"]}
    merged.update({key: value for key, value in (requested or {}).items() if value not in (None, "")})
    return build_agent_config(merged)


@app.post("/api/projects/{project_id}/sessions/reconstruction")
async def create_reconstruction_session(project_id: str, request: Request) -> dict[str, Any]:
    project = require_project(project_id)
    body = await request.json()
    if not ALLOW_RECONSTRUCTION:
        raise api_error(
            400,
            "The reconstruction agent runs on the Linux computer where CryoSPARC is installed. "
            "Start CryoPilot there to process data.",
        )
    form = dict(body.get("manifest") or {})
    form.setdefault("project_title", project["title"])
    form.setdefault("parent_directory", project["parent_directory"])
    try:
        manifest = build_manifest(form)
        agent_config = _reconstruction_agent_config(body.get("agent") or {})
    except ManifestError as exc:
        raise api_error(422, "Check the highlighted fields", errors=exc.errors)
    require_model_key(agent_config["provider"])
    settings = cryosparc_settings()
    if not settings["email"] or not settings["password_set"]:
        raise api_error(400, "Sign in to CryoSPARC in Settings before starting a session.", setup="cryosparc")
    agent_config["cryosparc_url"] = settings["url"]
    agent_config["cryosparc_email"] = settings["email"]
    guide = guides.normalize(body.get("processing_guide") or processing_guide())
    title = str(body.get("title") or "").strip() or manifest["user_goal"][:60]

    def start() -> dict[str, Any]:
        # File writes and the process start stay off the event loop.
        session = store.create_session(
            project_id=project["id"],
            kind="reconstruction",
            title=title,
            config={"manifest": manifest, "agent": agent_config, "processing_guide": guide},
            status="starting",
        )
        dirs = file_roots.ensure_project_dirs(project["slug"])
        session_dir = Path(dirs["reconstruction"]) / session["id"]
        session_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = session_dir / "manifest.json"
        config_path = session_dir / "agent.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        config_path.write_text(json.dumps(agent_config, indent=2, sort_keys=True), encoding="utf-8")
        try:
            processes.start_reconstruction(
                session["id"],
                cwd=session_dir,
                manifest_path=manifest_path,
                config_path=config_path,
                env=driver_env("reconstruction", guide=guide),
            )
        except Exception as exc:
            _start_failed(session["id"], exc)
            raise api_error(500, f"Could not start the reconstruction agent: {exc}")
        return session_runtime_view(store.get_session(session["id"]))

    return await run_in_threadpool(start)


@app.post("/api/projects/{project_id}/sessions/reconstruction/import")
async def import_reconstruction_session(project_id: str, request: Request) -> dict[str, Any]:
    project = require_project(project_id)
    body = await request.json()
    harness_id = str(body.get("harness_session_id") or "").strip()
    if not harness_id:
        raise api_error(400, "harness_session_id is required")
    existing = store.find_session_by_harness_id(harness_id)
    if existing is not None:
        raise api_error(409, "That session is already attached to a project", session_id=existing["id"])
    try:
        harness = trace_reader.session(harness_id)
    except TraceError as exc:
        raise api_error(404, str(exc))
    manifest = harness.get("manifest") or {}
    project_title = (manifest.get("project") or {}).get("title")
    title = str(body.get("title") or "").strip() or (project_title or manifest.get("user_goal") or harness_id)[:80]
    config = {
        "manifest": manifest,
        "imported": True,
        "project_uid": harness.get("project_uid"),
        "workspace_uid": harness.get("workspace_uid"),
    }
    # The harness does not record its guide; the caller may say which one the run used.
    guide = str(body.get("processing_guide") or "").strip().lower()
    if guide:
        if guide not in guides.GUIDE_FILES:
            raise api_error(422, f"Unknown processing guide: {guide}")
        config["processing_guide"] = guide
    session = store.create_session(
        project_id=project["id"],
        kind="reconstruction",
        title=title,
        harness_session_id=harness_id,
        config=config,
        status="stopped",
    )
    file_roots.ensure_project_dirs(project["slug"])
    return session_runtime_view(session)


def _modeling_options(body: dict[str, Any]) -> tuple[str, str, int]:
    saved = defaults()
    model = str(body.get("model") or saved["modeling_model"]).strip()
    effort = str(body.get("reasoning_effort") or saved["modeling_effort"]).strip().lower()
    if effort not in REASONING_EFFORTS:
        raise api_error(422, "Unknown reasoning effort", errors={"reasoning_effort": "Choose a valid effort"})
    try:
        max_turns = int(body.get("max_turns") or saved["modeling_max_turns"])
    except (TypeError, ValueError):
        raise api_error(422, "Maximum turns must be a whole number")
    return model, effort, max_turns


def _modeling_message(project: dict[str, Any], body: dict[str, Any], workspace_relative: str, text_key: str) -> str:
    """The text the modeling agent receives: the prompt plus its attached inputs."""

    prompt = str(body.get(text_key) or "").strip()
    if not prompt and body.get("objective"):
        # Older clients send the structured modeling form.
        return compose_modeling_request(body, workspace_relative)
    lines = attachments.materialize(project, body.get("attachments") or [])
    if lines:
        prompt = f"{prompt}\n\nInputs:\n" + "\n".join(f"- {line}" for line in lines)
    return prompt.strip()


@app.post("/api/projects/{project_id}/sessions/modeling")
async def create_modeling_session(project_id: str, request: Request) -> dict[str, Any]:
    project = require_project(project_id)
    body = await request.json()
    model, effort, max_turns = _modeling_options(body)
    require_model_key(provider_for_model(model))
    file_roots.ensure_project_dirs(project["slug"])
    workspace_relative = file_roots.modeling_workspace_relative(project["slug"])
    try:
        # Attaching a map copies (or downloads) hundreds of megabytes; keep it off the event loop.
        request_text = await run_in_threadpool(_modeling_message, project, body, workspace_relative, "prompt")
    except attachments.AttachmentError as exc:
        raise api_error(422, str(exc))
    if not request_text:
        raise api_error(422, "Describe what the modeling agent should do", errors={"prompt": "Required"})
    first_line = str(body.get("prompt") or body.get("objective") or "").strip().splitlines()
    title = str(body.get("title") or "").strip() or (first_line[0][:60] if first_line else "Modeling session")

    def start() -> dict[str, Any]:
        session = store.create_session(
            project_id=project["id"],
            kind="modeling",
            title=title,
            config={
                "model": model,
                "reasoning_effort": effort,
                "max_turns": max_turns,
                "workspace": workspace_relative,
                "attachments": body.get("attachments") or [],
            },
            status="starting",
        )
        try:
            processes.start_modeling(
                session["id"],
                workspace_relative=workspace_relative,
                model=model,
                effort=effort,
                max_turns=max_turns,
                env=driver_env("modeling"),
            )
            record = store.append_modeling_event(session["id"], "user", {"text": request_text})
            bus.publish(session["id"], {"type": "event", "event": record})
            processes.send_message(session["id"], request_text)
        except Exception as exc:
            _start_failed(session["id"], exc)
            raise api_error(500, f"Could not start the modeling agent: {exc}")
        return session_runtime_view(store.get_session(session["id"]))

    return await run_in_threadpool(start)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    return session_runtime_view(require_session(session_id))


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, request: Request) -> dict[str, Any]:
    session = require_session(session_id)
    body = await request.json()
    updates = {}
    if "title" in body:
        updates["title"] = str(body["title"]).strip() or session["title"]
    return session_runtime_view(store.update_session(session_id, **updates))


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    processes.stop(session["id"])
    store.delete_session(session["id"])
    return {"ok": True}


def _start_failed(session_id: str, exc: Exception) -> None:
    """Record a driver that could not start, so the session does not stay 'starting'."""

    store.update_session(session_id, status="error", last_error=str(exc))
    bus.publish(session_id, {"type": "status", "state": "error", "detail": str(exc), "running": False})


def _reconstruction_provider(session: dict[str, Any]) -> str | None:
    try:
        provider = trace_reader.session(session["harness_session_id"]).get("provider")
    except TraceError:
        provider = None
    return provider or ((session.get("config") or {}).get("agent") or {}).get("provider")


def _ensure_reconstruction_process(session: dict[str, Any]) -> None:
    if processes.is_alive(session["id"]):
        return
    harness_id = session.get("harness_session_id")
    if not harness_id:
        raise api_error(409, "This session never started; create a new session instead.")
    if not ALLOW_RECONSTRUCTION:
        raise api_error(400, "The reconstruction agent runs on the Linux computer where CryoSPARC is installed.")
    settings = cryosparc_settings()
    if not settings["email"] or not settings["password_set"]:
        raise api_error(400, "Sign in to CryoSPARC in Settings before resuming.", setup="cryosparc")
    require_model_key(_reconstruction_provider(session))
    project = store.get_project(session["project_id"])
    dirs = file_roots.ensure_project_dirs(project["slug"])
    session_dir = Path(dirs["reconstruction"]) / session["id"]
    # A session keeps its guide. Imported runs and sessions from before the
    # setting existed ran with the harness default, so they resume with it.
    config = dict(session.get("config") or {})
    guide = guides.normalize(config.get("processing_guide"))
    if config.get("processing_guide") != guide:
        config["processing_guide"] = guide
        store.update_session(session["id"], config=config)
    store.update_session(session["id"], status="starting", last_error=None)
    bus.publish(session["id"], {"type": "status", "state": "starting", "detail": "Starting the agent", "running": True})
    try:
        processes.start_reconstruction(
            session["id"],
            cwd=session_dir,
            resume_harness_id=harness_id,
            env=driver_env("reconstruction", guide=guide),
        )
    except Exception as exc:
        _start_failed(session["id"], exc)
        raise


def _ensure_modeling_process(session: dict[str, Any]) -> None:
    if processes.is_alive(session["id"]):
        return
    config = session.get("config") or {}
    saved = defaults()
    model = config.get("model") or saved["modeling_model"]
    require_model_key(provider_for_model(model))
    store.update_session(session["id"], status="starting", last_error=None)
    bus.publish(session["id"], {"type": "status", "state": "starting", "detail": "Starting the agent", "running": True})
    try:
        processes.start_modeling(
            session["id"],
            workspace_relative=config.get("workspace") or "workspaces/default",
            model=model,
            effort=config.get("reasoning_effort") or saved["modeling_effort"],
            max_turns=int(config.get("max_turns") or saved["modeling_max_turns"]),
            env=driver_env("modeling"),
        )
    except Exception as exc:
        _start_failed(session["id"], exc)
        raise


@app.post("/api/sessions/{session_id}/start")
def start_session(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    try:
        if session["kind"] == "reconstruction":
            _ensure_reconstruction_process(session)
        else:
            _ensure_modeling_process(session)
    except HTTPException:
        raise
    except Exception as exc:
        raise api_error(500, f"Could not start the agent: {exc}")
    return session_runtime_view(store.get_session(session_id))


@app.post("/api/sessions/{session_id}/messages")
async def post_message(session_id: str, request: Request) -> dict[str, Any]:
    session = require_session(session_id)
    body = await request.json()
    runtime = processes.status(session_id)
    if runtime["running"] and runtime["state"] in {"running", "connecting", "initializing", "starting"}:
        raise api_error(409, "The agent is still working. Wait for it to finish or stop it first.")
    try:
        if session["kind"] == "modeling":
            project = store.get_project(session["project_id"])
            workspace_relative = (session.get("config") or {}).get("workspace") or file_roots.modeling_workspace_relative(project["slug"])
            content = await run_in_threadpool(_modeling_message, project, body, workspace_relative, "content")
        else:
            content = str(body.get("content") or "").strip()
    except attachments.AttachmentError as exc:
        raise api_error(422, str(exc))
    if not content:
        raise api_error(400, "Message is empty")

    def send() -> dict[str, Any]:
        # Starting a driver forks a process; keep it off the event loop.
        try:
            if session["kind"] == "reconstruction":
                _ensure_reconstruction_process(session)
            else:
                _ensure_modeling_process(session)
                record = store.append_modeling_event(session_id, "user", {"text": content})
                bus.publish(session_id, {"type": "event", "event": record})
            processes.send_message(session_id, content)
        except HTTPException:
            raise
        except Exception as exc:
            raise api_error(500, f"Could not send the message: {exc}")
        store.update_session(session_id, status="running")
        bus.publish(session_id, {"type": "status", "state": "running", "detail": "Message sent", "running": True})
        return session_runtime_view(store.get_session(session_id))

    return await run_in_threadpool(send)


@app.post("/api/sessions/{session_id}/interrupt")
def interrupt_session(session_id: str) -> dict[str, Any]:
    require_session(session_id)
    processes.interrupt(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: str) -> dict[str, Any]:
    require_session(session_id)
    processes.stop(session_id)
    store.update_session(session_id, status="stopped")
    bus.publish(session_id, {"type": "status", "state": "stopped", "detail": "Agent process stopped", "running": False})
    return session_runtime_view(store.get_session(session_id))


# ------------------------------------------------------------------- trace
# Raster images only are shown inline. Anything else (an SVG the agent looked
# at, say) could carry script, so it is offered as a download. Both get the
# no-script policy CryoSPARC assets use.
INLINE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _image_response(mime: str, data: bytes) -> Response:
    headers = {**routes_cryosparc.ASSET_HEADERS, "Cache-Control": "private, max-age=3600"}
    mime = mime.lower()
    if mime not in INLINE_IMAGE_TYPES:
        mime = "application/octet-stream"
        headers["Content-Disposition"] = "attachment"
    return Response(content=data, media_type=mime, headers=headers)


@app.get("/api/sessions/{session_id}/trace")
def get_trace(session_id: str, after: int = 0) -> dict[str, Any]:
    session = require_session(session_id)
    if session["kind"] == "modeling":
        events = store.list_modeling_events(session_id, after)
        return {"kind": "modeling", "events": events, "cursor": events[-1]["id"] if events else after}
    harness_id = session.get("harness_session_id")
    if not harness_id:
        return {"kind": "reconstruction", "items": [], "cursor": after, "recursive_calls": []}
    try:
        items = trace_reader.items(harness_id, after)
        recursive_calls = trace_reader.recursive_calls(harness_id)
    except TraceError as exc:
        raise api_error(404, str(exc))
    return {
        "kind": "reconstruction",
        "items": items,
        "cursor": items[-1]["id"] if items else after,
        "recursive_calls": recursive_calls,
    }


@app.get("/api/sessions/{session_id}/items/{item_id}")
def get_item(session_id: str, item_id: int) -> Any:
    session = require_session(session_id)
    if not session.get("harness_session_id"):
        raise api_error(404, "No transcript")
    try:
        return {"payload": trace_reader.item_payload(session["harness_session_id"], item_id)}
    except TraceError as exc:
        raise api_error(404, str(exc))


@app.get("/api/sessions/{session_id}/items/{item_id}/image/{index}")
def get_item_image(session_id: str, item_id: int, index: int) -> Response:
    session = require_session(session_id)
    if not session.get("harness_session_id"):
        raise api_error(404, "No transcript")
    try:
        mime, data = trace_reader.item_image(session["harness_session_id"], item_id, index)
    except TraceError as exc:
        raise api_error(404, str(exc))
    return _image_response(mime, data)


@app.get("/api/sessions/{session_id}/recursive/{call_id}/items/{item_id}/image/{index}")
def get_recursive_item_image(session_id: str, call_id: int, item_id: int, index: int) -> Response:
    session = require_session(session_id)
    if not session.get("harness_session_id"):
        raise api_error(404, "No transcript")
    try:
        mime, data = trace_reader.recursive_item_image(session["harness_session_id"], call_id, item_id, index)
    except TraceError as exc:
        raise api_error(404, str(exc))
    return _image_response(mime, data)


@app.get("/api/sessions/{session_id}/recursive/{call_id}")
def get_recursive_items(session_id: str, call_id: int) -> dict[str, Any]:
    session = require_session(session_id)
    if not session.get("harness_session_id"):
        raise api_error(404, "No transcript")
    try:
        return {"items": trace_reader.recursive_items(session["harness_session_id"], call_id)}
    except TraceError as exc:
        raise api_error(404, str(exc))


@app.get("/api/sessions/{session_id}/jobs")
def get_known_jobs(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    if not session.get("harness_session_id"):
        return {"jobs": []}
    try:
        return {"jobs": trace_reader.known_jobs(session["harness_session_id"])}
    except TraceError:
        return {"jobs": []}


@app.get("/api/sessions/{session_id}/console")
def get_console(session_id: str, after: int = 0) -> dict[str, Any]:
    require_session(session_id)
    lines = store.list_console_lines(session_id, after)
    return {"lines": lines, "cursor": lines[-1]["id"] if lines else after}


@app.get("/api/sessions/{session_id}/events")
def session_events(session_id: str, request: Request, after: int = 0) -> StreamingResponse:
    session = require_session(session_id)
    subscription = bus.subscribe(session_id)

    def encode(event: dict[str, Any]) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    def generate():
        cursor = after
        last_poll = 0.0
        try:
            runtime = processes.status(session_id)
            yield encode({"type": "status", "state": runtime["state"], "detail": runtime["detail"], "running": runtime["running"]})
            while not shutting_down.is_set():
                now = time.time()
                if session["kind"] == "reconstruction" and now - last_poll >= 1.0:
                    last_poll = now
                    current = store.get_session(session_id)
                    harness_id = current.get("harness_session_id")
                    if harness_id:
                        try:
                            items = trace_reader.items(harness_id, cursor)
                        except TraceError:
                            items = []
                        if items:
                            cursor = items[-1]["id"]
                            yield encode({"type": "trace", "items": items, "cursor": cursor})
                try:
                    event = subscription.get(timeout=1.0)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield encode(event)
        finally:
            bus.unsubscribe(session_id, subscription)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ----------------------------------------------------------- harness list
@app.get("/api/harness/sessions")
def list_harness_sessions() -> dict[str, Any]:
    try:
        sessions = trace_reader.list_sessions()
    except TraceError:
        return {"sessions": []}
    for session in sessions:
        existing = store.find_session_by_harness_id(session["id"])
        session["attached_project_id"] = existing["project_id"] if existing else None
        session["attached_session_id"] = existing["id"] if existing else None
    return {"sessions": sessions}


# ------------------------------------------------------------------ static
@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail.get("message"), **detail})


@app.get("/{full_path:path}")
def spa(full_path: str) -> Response:
    if full_path.startswith("api/"):
        raise api_error(404, "Unknown API route")
    if not FRONTEND_DIST.is_dir():
        return Response(
            "The frontend has not been built. Run `python app/serve.py` (it builds automatically) "
            "or `cd app/frontend && npm install && npm run build`.\n",
            media_type="text/plain",
            status_code=503,
        )
    candidate = (FRONTEND_DIST / full_path).resolve() if full_path else FRONTEND_DIST / "index.html"
    try:
        candidate.relative_to(FRONTEND_DIST.resolve())
    except ValueError:
        raise api_error(400, "Invalid path")
    if candidate.is_file():
        # Only Vite's hashed bundles under assets/ never change; public files
        # such as molstar.js keep their names across releases.
        immutable = full_path.startswith("assets/")
        headers = {"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"}
        return FileResponse(str(candidate), headers=headers)
    return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-cache"})
