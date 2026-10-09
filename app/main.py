from __future__ import annotations

import hashlib
import hmac
import json
import base64
import logging
import mimetypes
import multiprocessing
import os
import re
import signal
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent import run as agent_run, stream_run as agent_stream_run

from .config import BASE_DIR, load_config
from .markdown import render_markdown
from .security import hash_token, new_token, now_ts, sign_payload, verify_payload
from .store import Store
from .unified_config import (
    DEFAULT_CONFIG_PATH,
    FIELD_SPECS,
    SECRET_FIELDS,
    bootstrap_config,
    get_path,
    load_unified_config,
    masked,
    save_unified_config,
    set_path,
)


logger = logging.getLogger("hermes.portal")

# A fresh deployment has no config file yet. Write one with randomly generated access
# codes and print them once: this log line is the only place an operator can read them
# before logging in and changing them on the config page.
_generated_credentials = bootstrap_config()
if _generated_credentials:
    logger.warning("Wrote %s with generated credentials, shown once:", DEFAULT_CONFIG_PATH)
    for _field, _token in _generated_credentials.items():
        logger.warning("    %s = %s", _field, _token)

config = load_config()
_config_mtime = DEFAULT_CONFIG_PATH.stat().st_mtime if DEFAULT_CONFIG_PATH.exists() else None
store = Store(config.database_path)
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["markdown"] = render_markdown

app = FastAPI(title=config.app_name)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


_session_processes: dict[str, multiprocessing.Process] = {}


def _active_process(session_id: str) -> multiprocessing.Process | None:
    proc = _session_processes.get(session_id)
    if proc and proc.is_alive():
        return proc
    if proc:
        _session_processes.pop(session_id, None)
    return None


# --- SSE stream runs with reconnect support -------------------------------
#
# Each /chat/{sid}/stream request either starts a new agent run (background
# thread that pushes events into a per-session ring buffer) or attaches to an
# in-progress run. EventSource auto-reconnects send `Last-Event-ID`; we replay
# any buffered events past that id so the client picks up where it left off.

STREAM_BUFFER_LIMIT = int(os.getenv("HERMES_STREAM_BUFFER_LIMIT", "1024"))
STREAM_RUN_GRACE_SEC = float(os.getenv("HERMES_STREAM_RUN_GRACE_SEC", "60"))
STREAM_TAIL_KEEPALIVE_SEC = float(os.getenv("HERMES_STREAM_TAIL_KEEPALIVE_SEC", "15"))
STREAM_RETRY_MS = int(os.getenv("HERMES_STREAM_RETRY_MS", "2000"))


class StreamRun:
    """Per-session record of an in-flight agent run plus its event buffer."""

    __slots__ = (
        "session_id",
        "run_id",
        "user_text",
        "events",
        "next_id",
        "done",
        "final_text",
        "started_at",
        "cond",
    )

    def __init__(self, session_id: str, user_text: str) -> None:
        self.session_id = session_id
        self.run_id = uuid4().hex
        self.user_text = user_text
        self.events: list[tuple[int, str, dict]] = []
        self.next_id = 0
        self.done = False
        self.final_text = ""
        self.started_at = time.time()
        self.cond = threading.Condition()

    def push(self, kind: str, data: dict) -> int:
        with self.cond:
            event_id = self.next_id
            self.next_id += 1
            self.events.append((event_id, kind, data))
            if len(self.events) > STREAM_BUFFER_LIMIT:
                # Drop the oldest half to keep the buffer bounded.
                self.events = self.events[-STREAM_BUFFER_LIMIT // 2:]
            self.cond.notify_all()
            return event_id

    def finish(self) -> None:
        with self.cond:
            self.done = True
            self.cond.notify_all()


_stream_runs: dict[str, StreamRun] = {}
_stream_runs_lock = threading.Lock()


def _active_stream_run(session_id: str) -> StreamRun | None:
    with _stream_runs_lock:
        return _stream_runs.get(session_id)


def _drop_stream_run(session_id: str, run: StreamRun) -> None:
    with _stream_runs_lock:
        if _stream_runs.get(session_id) is run:
            _stream_runs.pop(session_id, None)


def _format_sse_event(event_id: int, event_type: str, data: dict) -> str:
    return (
        f"id: {event_id}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    )


def _refresh_config_if_changed() -> None:
    global config, _config_mtime
    mtime = DEFAULT_CONFIG_PATH.stat().st_mtime if DEFAULT_CONFIG_PATH.exists() else None
    if mtime != _config_mtime:
        config = load_config()
        _config_mtime = mtime


@app.middleware("http")
async def refresh_config_middleware(request: Request, call_next):
    _refresh_config_if_changed()
    return await call_next(request)


def _hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token_matches(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_hash_value(token), expected_hash)


def _safe_name(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:120] or "upload.bin"


def _session_root(session_id: str) -> Path:
    root = config.workspace_path / "sessions" / session_id
    root.mkdir(parents=True, exist_ok=True)
    for child in ("uploads", "outputs", "work", "logs"):
        (root / child).mkdir(exist_ok=True)
    return root


def _rel_under_session(session_id: str, path: Path) -> str:
    root = _session_root(session_id).resolve()
    resolved = path.resolve()
    if root not in resolved.parents and resolved != root:
        raise HTTPException(status_code=400, detail="Invalid file path")
    return str(resolved.relative_to(root))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_auth(request: Request) -> dict | None:
    token = request.cookies.get(config.session_cookie_name)
    if not token:
        return None
    return verify_payload(token, config.secret_key, max_age_seconds=86400)


def current_context(request: Request, require_approved: bool = False) -> dict:
    auth = _current_auth(request)
    if not auth:
        raise HTTPException(status_code=401, detail="Not logged in")
    user_label = str(auth.get("user_label", "")).strip()
    role = str(auth.get("role", "user"))
    if not user_label:
        raise HTTPException(status_code=401, detail="Invalid session")

    device_token = request.cookies.get(config.device_cookie_name)
    device = store.device_by_hash(hash_token(device_token)) if device_token else None
    if not device:
        raise HTTPException(status_code=403, detail="Device is not registered")
    if require_approved and role != "admin" and device["status"] != "approved":
        raise HTTPException(status_code=403, detail="Device is not approved")
    return {
        "user_label": user_label,
        "role": role,
        "admin": role == "admin",
        "device_token": device_token,
        "device": device,
    }


def _ensure_device(request: Request, user_label: str, role: str) -> RedirectResponse | None:
    device_token = request.cookies.get(config.device_cookie_name) or new_token()
    device = store.device_by_hash(hash_token(device_token))
    if not device:
        store.create_device(hash_token(device_token), user_label, user_label, now_ts())
        device = store.device_by_hash(hash_token(device_token))
    if role == "admin" and device and device["status"] != "approved":
        store.set_device_status(int(device["id"]), "approved", now_ts())
    return None


def _redirect_with_cookies(user_label: str, role: str, device_token: str | None) -> RedirectResponse:
    redirect = RedirectResponse("/", status_code=303)
    redirect.set_cookie(
        config.session_cookie_name,
        sign_payload({"user_label": user_label, "role": role, "iat": int(time.time())}, config.secret_key),
        httponly=True,
        secure=config.public_base_url.startswith("https://"),
        samesite="lax",
        max_age=86400,
    )
    if device_token:
        redirect.set_cookie(
            config.device_cookie_name,
            device_token,
            httponly=True,
            secure=config.public_base_url.startswith("https://"),
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
        )
    return redirect


def _get_or_create_active_session(ctx: dict) -> str | None:
    ts = now_ts()
    store.expire_sessions(ts)
    device_id = int(ctx["device"]["id"])
    active = store.active_sessions_for_device(device_id)
    if active:
        session_id = str(active[0]["id"])
        store.touch_session(
            session_id,
            ts,
            ts + config.session_ttl_minutes * 60,
        )
        return session_id
    if (
        not ctx["admin"]
        and len(store.active_sessions_for_user(ctx["user_label"])) >= config.max_sessions_per_user
    ):
        return None
    if store.active_session_count() >= config.max_active_sessions:
        return None
    session_id = uuid4().hex
    store.create_session(
        session_id,
        device_id,
        ctx["user_label"],
        ts,
        ts + config.session_ttl_minutes * 60,
    )
    _session_root(session_id)
    store.add_message(session_id, "system", "Hermes session created.", ts)
    return session_id


def _create_session(ctx: dict) -> str | None:
    ts = now_ts()
    store.expire_sessions(ts)
    if store.active_session_count() >= config.max_active_sessions:
        return None
    session_id = uuid4().hex
    store.create_session(
        session_id,
        int(ctx["device"]["id"]),
        ctx["user_label"],
        ts,
        ts + config.session_ttl_minutes * 60,
    )
    _session_root(session_id)
    store.add_message(session_id, "system", "Hermes session created.", ts)
    return session_id


def _require_session_for_device(session_id: str, ctx: dict) -> dict:
    store.expire_sessions(now_ts())
    session = store.session_by_id(session_id)
    if not session or session["status"] != "active":
        raise HTTPException(status_code=404, detail="Session not active")
    if int(session["device_id"]) != int(ctx["device"]["id"]) and not ctx["admin"]:
        raise HTTPException(status_code=403, detail="Session does not belong to this device")
    return dict(session)


def _require_visible_session(session_id: str, ctx: dict) -> dict:
    store.expire_sessions(now_ts())
    session = store.session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if str(session["user_label"]) != ctx["user_label"] and not ctx["admin"]:
        raise HTTPException(status_code=403, detail="Session does not belong to this user")
    return dict(session)


def _session_list(ctx: dict, selected_session_id: str) -> list[dict]:
    rows = store.list_sessions_for_user(ctx["user_label"])
    sessions = []
    for row in rows:
        title = (row["title"] or "").strip() or "新会话"
        sessions.append(
            {
                "id": row["id"],
                "status": row["status"],
                "title": title[:80],
                "message_count": int(row["message_count"] or 0),
                "updated_at": row["updated_at"],
                "running": _active_process(str(row["id"])) is not None,
                "is_current": row["id"] == selected_session_id,
            }
        )
    return sessions


def _register_outputs(session_id: str) -> None:
    existing = {row["rel_path"] for row in store.list_files(session_id)}
    outputs = _session_root(session_id) / "outputs"
    for path in outputs.rglob("*"):
        if not path.is_file():
            continue
        rel_path = _rel_under_session(session_id, path)
        if rel_path in existing:
            continue
        store.add_file(
            session_id,
            "output",
            path.name,
            rel_path,
            path.stat().st_size,
            _file_sha256(path),
            now_ts(),
        )


def _write_session_recovery(session_id: str, label: str, content: str) -> Path | None:
    try:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        safe_label = re.sub(r"[^A-Za-z0-9_.-]", "-", label).strip("-") or "recovery"
        path = _session_root(session_id) / "outputs" / f"hermes-{safe_label}-{ts}.md"
        path.write_text(content.strip() + "\n", encoding="utf-8")
        return path
    except OSError:
        return None


def _session_file_context(session_id: str) -> str:
    files = store.list_files(session_id)
    if not files:
        return ""
    lines = ["当前会话文件："]
    for row in files:
        lines.append(f"- {row['kind']}: {row['rel_path']} ({row['size']} bytes)")
    lines.append("")
    lines.append("容器内当前会话目录结构：uploads/、work/、outputs/、logs/。")
    lines.append("需要产出可下载文件时，请把结果写入 outputs/。")
    return "\n".join(lines)


def _session_image_inputs(session_id: str) -> list[dict[str, str]]:
    max_images = int(os.getenv("HERMES_MULTIMODAL_MAX_IMAGES", "4"))
    max_bytes = int(os.getenv("HERMES_MULTIMODAL_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    images: list[dict[str, str]] = []
    for row in store.list_files(session_id):
        if row["kind"] != "upload":
            continue
        rel_path = str(row["rel_path"])
        if Path(rel_path).suffix.lower() not in image_exts:
            continue
        if int(row["size"] or 0) > max_bytes:
            continue
        path = _session_root(session_id) / rel_path
        try:
            _rel_under_session(session_id, path)
            data = path.read_bytes()
        except OSError:
            continue
        media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        encoded = base64.b64encode(data).decode("ascii")
        images.append({"url": f"data:{media_type};base64,{encoded}", "filename": str(row["filename"])})
        if len(images) >= max_images:
            break
    return images


def _wants_search(content: str) -> bool:
    text = content.lower()
    markers = ("搜索", "联网", "查找", "最新", "今天", "新闻", "资料", "source", "search", "latest", "recent")
    return any(marker in text for marker in markers)


def _agent_history_messages(session_id: str, current_user_text: str) -> list[dict[str, str]]:
    try:
        max_messages = int(os.getenv("HERMES_AGENT_HISTORY_MESSAGES", "80"))
    except ValueError:
        max_messages = 80
    max_messages = max(0, max_messages)
    rows = list(store.list_messages(session_id))
    if rows and rows[-1]["role"] == "user" and str(rows[-1]["content"]).strip() == current_user_text.strip():
        rows = rows[:-1]
    rows = rows[-max_messages:]
    history: list[dict[str, str]] = []
    for row in rows:
        role = str(row["role"])
        if role == "user":
            mapped_role = "user"
        elif role == "hermes":
            mapped_role = "assistant"
        else:
            continue
        content = str(row["content"] or "").strip()
        if content and content != "Hermes session created.":
            history.append({"role": mapped_role, "content": content})
    return history


def _ask_hermes_agent(session_id: str, content: str) -> str:
    try:
        answer = agent_run(**_build_agent_kwargs(session_id, content))
    except Exception as exc:
        return f"Agent 调用失败：{exc}"
    _register_outputs(session_id)
    return answer


def _handle_hermes_request(session_id: str, content: str) -> str:
    return _ask_hermes_agent(session_id, content)


def _build_agent_kwargs(session_id: str, content: str) -> dict:
    """Shared setup for streaming and non-streaming agent invocation."""
    context = _session_file_context(session_id)
    user_content = content.strip()
    if context:
        user_content = f"{context}\n\n用户需求：\n{user_content}"

    extra_system = None
    if config.search_enabled and _wants_search(content):
        extra_system = (
            "当用户要求搜索、最新信息、查找资料或引用来源时，调用 web_search 工具并在回答中给出来源 URL。"
        )
        if config.search_require_sources:
            extra_system += " 如果无法提供来源，请明确说明没有可用来源。"

    skills_dir = Path(os.getenv("HERMES_SKILLS_DIR", str(BASE_DIR / "skills")))
    return {
        "session_id": session_id,
        "user_message": user_content,
        "db_path": config.database_path,
        "skills_dir": skills_dir,
        "extra_system": extra_system,
        "image_inputs": _session_image_inputs(session_id),
        "history_messages": _agent_history_messages(session_id, content),
    }


def _run_session_process(session_id: str, user_text: str, kwargs: dict) -> None:
    child_store = Store(config.database_path)
    final_text = ""

    def _handle_sigterm(signum, frame) -> None:
        nonlocal final_text
        if not final_text:
            final_text = "Session terminated before Hermes produced a final answer."
        _write_session_recovery(
            session_id,
            "terminated",
            "# Hermes termination recovery\n\n"
            "The background agent process received SIGTERM before it completed.\n\n"
            "## Last known message\n\n"
            f"{final_text}",
        )
        raise SystemExit(143)

    previous_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        for event in agent_stream_run(**kwargs):
            kind = event.get("type", "message")
            if kind == "final":
                final_text = event.get("content", "")
            elif kind in ("timeout", "max_iters", "provider_error"):
                final_text = event.get("message", "")
            elif kind == "done":
                final_text = event.get("final", final_text)
    except SystemExit:
        raise
    except BaseException as exc:
        final_text = f"Agent 调用失败：{exc}"
        _write_session_recovery(session_id, "agent-crash", f"# Hermes agent crash\n\n{final_text}")
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        try:
            if final_text.startswith(("[hermes-timeout]", "[hermes-max-iterations]", "[hermes-provider-failure]")):
                _write_session_recovery(session_id, "final-state", f"# Hermes final state\n\n{final_text}")
            child_store.add_message(session_id, "hermes", final_text or "(empty)", now_ts())
            child_store.touch_session(
                session_id,
                now_ts(),
                now_ts() + config.session_ttl_minutes * 60,
            )
            _register_outputs(session_id)
        except BaseException:
            pass


def _start_session_process(session_id: str, user_text: str) -> None:
    if _active_process(session_id):
        raise HTTPException(status_code=409, detail="Session is already running")
    kwargs = _build_agent_kwargs(session_id, user_text)
    proc = multiprocessing.Process(
        target=_run_session_process,
        args=(session_id, user_text, kwargs),
        daemon=True,
    )
    proc.start()
    _session_processes[session_id] = proc


@app.on_event("startup")
def on_startup() -> None:
    store.init()
    config.workspace_path.mkdir(parents=True, exist_ok=True)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    auth = _current_auth(request)
    if not auth:
        return templates.TemplateResponse(request, "login.html", {"config": config, "admin": False})
    ctx = current_context(request)
    store.update_last_seen(hash_token(ctx["device_token"]), now_ts())
    if ctx["admin"]:
        return RedirectResponse("/chat", status_code=303)
    if ctx["device"]["status"] != "approved":
        return templates.TemplateResponse(
            request,
            "pending.html",
            {"config": config, "ctx": ctx, "admin": ctx["admin"]},
        )
    return RedirectResponse("/chat", status_code=303)


@app.post("/login")
def login(
    request: Request,
    user_label: Annotated[str, Form(min_length=1, max_length=80)],
    access_token: Annotated[str, Form(min_length=1)],
) -> RedirectResponse:
    user_label = user_label.strip()
    role = ""
    if _token_matches(access_token, config.admin_token_hash):
        role = "admin"
    elif _token_matches(access_token, config.access_token_hash):
        role = "user"
    else:
        raise HTTPException(status_code=403, detail="Invalid access token")

    device_token = request.cookies.get(config.device_cookie_name) or new_token()
    store.create_device(hash_token(device_token), user_label, user_label, now_ts())
    if role == "admin":
        device = store.device_by_hash(hash_token(device_token))
        if device:
            store.set_device_status(int(device["id"]), "approved", now_ts())
    store.audit("login", user_label, role, now_ts())
    return _redirect_with_cookies(user_label, role, device_token)


@app.get("/chat", response_class=HTMLResponse)
def chat(request: Request) -> HTMLResponse:
    ctx = current_context(request, require_approved=True)
    session_id = _get_or_create_active_session(ctx)
    if not session_id:
        return templates.TemplateResponse(
            request,
            "busy.html",
            {"config": config, "ctx": ctx, "admin": ctx["admin"]},
            status_code=429,
        )
    _register_outputs(session_id)
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "config": config,
            "ctx": ctx,
            "admin": ctx["admin"],
            "session_id": session_id,
            "session": store.session_by_id(session_id),
            "running": _active_process(session_id) is not None,
            "sessions": _session_list(ctx, session_id),
            "messages": store.list_messages(session_id),
            "files": store.list_files(session_id),
        },
    )


@app.post("/chat/new")
def new_chat(request: Request) -> RedirectResponse:
    ctx = current_context(request, require_approved=True)
    session_id = _create_session(ctx)
    if not session_id:
        return RedirectResponse("/chat", status_code=303)
    store.audit("session_created", ctx["user_label"], session_id, now_ts())
    return RedirectResponse(f"/chat/{session_id}", status_code=303)


@app.get("/chat/{session_id}", response_class=HTMLResponse)
def chat_session(request: Request, session_id: str) -> HTMLResponse:
    ctx = current_context(request, require_approved=True)
    session = _require_visible_session(session_id, ctx)
    _register_outputs(session_id)
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "config": config,
            "ctx": ctx,
            "admin": ctx["admin"],
            "session_id": session_id,
            "session": session,
            "running": _active_process(session_id) is not None,
            "sessions": _session_list(ctx, session_id),
            "messages": store.list_messages(session_id),
            "files": store.list_files(session_id),
        },
    )


@app.post("/chat/{session_id}/message", response_model=None)
def send_message(
    request: Request,
    session_id: str,
    content: Annotated[str, Form(min_length=1, max_length=8000)],
) -> RedirectResponse | JSONResponse:
    ctx = current_context(request, require_approved=True)
    _require_session_for_device(session_id, ctx)
    if _active_process(session_id):
        raise HTTPException(status_code=409, detail="Session is already running")
    ts = now_ts()
    user_text = content.strip()
    store.add_message(session_id, "user", user_text, ts)
    store.touch_session(session_id, ts, ts + config.session_ttl_minutes * 60)
    _start_session_process(session_id, user_text)
    store.audit("message_started", ctx["user_label"], session_id, ts)
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"status": "started", "session_id": session_id})
    return RedirectResponse(f"/chat/{session_id}", status_code=303)


@app.get("/chat/{session_id}/stream")
def stream_message(
    request: Request,
    session_id: str,
    content: Annotated[str, Query(min_length=1, max_length=8000)],
) -> StreamingResponse:
    """SSE endpoint that streams agent events as they happen.

    Persists the user message immediately. Persists the final assistant
    message after the stream completes so a /chat reload sees it.
    """
    ctx = current_context(request, require_approved=True)
    _require_session_for_device(session_id, ctx)

    last_event_id_raw = request.headers.get("Last-Event-ID") or ""
    last_event_id: int | None = None
    if last_event_id_raw.isdigit():
        last_event_id = int(last_event_id_raw)
    is_reconnect = last_event_id is not None

    user_text = content.strip()
    existing = _active_stream_run(session_id)

    # Treat a request as a "resume" when we already have a run for this session
    # AND either the client signalled a reconnect via Last-Event-ID, or the
    # content matches the in-flight run (browsers replay the same URL on retry).
    resume = existing is not None and (
        is_reconnect or existing.user_text == user_text
    )

    if resume:
        run = existing  # type: ignore[assignment]
    elif existing is not None and not existing.done:
        # An in-flight run exists but this isn't a reconnect and the content
        # doesn't match. Reject rather than abandoning the running agent.
        raise HTTPException(status_code=409, detail="Session has an active run; please wait")
    else:
        ts = now_ts()
        store.add_message(session_id, "user", user_text, ts)
        store.audit("message_stream", ctx["user_label"], session_id, ts)
        kwargs = _build_agent_kwargs(session_id, user_text)
        run = StreamRun(session_id, user_text)
        with _stream_runs_lock:
            prior = _stream_runs.get(session_id)
            if prior is not None and not prior.done:
                # A concurrent request already started a run between our checks.
                run = prior
            else:
                _stream_runs[session_id] = run
                threading.Thread(
                    target=_run_agent_in_background,
                    args=(run, kwargs),
                    name=f"hermes-agent-{session_id[:8]}",
                    daemon=True,
                ).start()

    return StreamingResponse(
        _tail_stream_run(run, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable buffering on nginx if it's in front
            "Connection": "keep-alive",
        },
    )


def _run_agent_in_background(run: StreamRun, kwargs: dict) -> None:
    """Drive agent_stream_run, push every event into run's buffer.

    Survives client disconnect — the buffer keeps growing so a reconnecting
    EventSource can replay missed events. Persists the final assistant message
    regardless of whether anyone is listening.
    """
    final_text = ""
    try:
        for event in agent_stream_run(**kwargs):
            kind = event.get("type", "message")
            if kind == "tool_result":
                out = event.get("stdout") or ""
                err = event.get("stderr") or ""
                if len(out) > 4000:
                    event = {**event, "stdout": out[:4000] + f"\n...[truncated {len(out)-4000} bytes]"}
                if len(err) > 4000:
                    event = {**event, "stderr": err[:4000] + f"\n...[truncated {len(err)-4000} bytes]"}
            if kind == "final":
                final_text = event.get("content", "")
            elif kind in ("timeout", "max_iters", "provider_error"):
                final_text = event.get("message", "")
            elif kind == "done":
                final_text = event.get("final", final_text)
            run.push(kind, event)
    except Exception as exc:
        err_msg = f"Agent stream crashed: {exc}"
        final_text = err_msg
        _write_session_recovery(run.session_id, "stream-crash", f"# Hermes stream crash\n\n{err_msg}")
        run.push("provider_error", {"type": "provider_error", "message": err_msg})
        run.push("done", {"type": "done", "final": err_msg})
    finally:
        run.final_text = final_text
        run.finish()
        try:
            done_ts = now_ts()
            if final_text.startswith(("[hermes-timeout]", "[hermes-max-iterations]", "[hermes-provider-failure]")):
                _write_session_recovery(run.session_id, "final-state", f"# Hermes final state\n\n{final_text}")
            store.add_message(run.session_id, "hermes", final_text or "(empty)", done_ts)
            store.touch_session(
                run.session_id,
                done_ts,
                done_ts + config.session_ttl_minutes * 60,
            )
            _register_outputs(run.session_id)
        except Exception:
            pass
        threading.Timer(STREAM_RUN_GRACE_SEC, _drop_stream_run, args=(run.session_id, run)).start()


def _tail_stream_run(run: StreamRun, since: int | None) -> Iterator[str]:
    """Yield SSE-formatted events from `run`, starting after event id `since`.

    Replays any already-buffered events first (the reconnect case), then waits
    on the run's condition variable for new ones. Sends heartbeats so proxies
    don't drop the long-lived connection.
    """
    yield f"retry: {STREAM_RETRY_MS}\n\n"
    delivered = since if since is not None else -1
    while True:
        with run.cond:
            pending = [e for e in run.events if e[0] > delivered]
            is_done = run.done
        for event_id, event_type, event_data in pending:
            yield _format_sse_event(event_id, event_type, event_data)
            delivered = event_id
        if is_done:
            # Drain anything that landed between the snapshot and now.
            with run.cond:
                pending = [e for e in run.events if e[0] > delivered]
            for event_id, event_type, event_data in pending:
                yield _format_sse_event(event_id, event_type, event_data)
                delivered = event_id
            return
        with run.cond:
            run.cond.wait(timeout=STREAM_TAIL_KEEPALIVE_SEC)
        yield ": keepalive\n\n"


@app.post("/chat/{session_id}/upload")
async def upload_file(
    request: Request,
    session_id: str,
    file: UploadFile = File(...),
) -> RedirectResponse:
    ctx = current_context(request, require_approved=True)
    _require_session_for_device(session_id, ctx)
    files = store.list_files(session_id)
    upload_count = sum(1 for row in files if row["kind"] == "upload")
    if upload_count >= config.max_files_per_session:
        raise HTTPException(status_code=400, detail="Too many files in this session")

    filename = _safe_name(file.filename or "upload.bin")
    if Path(filename).suffix.lower() not in config.allowed_upload_extensions:
        raise HTTPException(status_code=400, detail="File extension is not allowed")

    root = _session_root(session_id)
    target = root / "uploads" / filename
    if target.exists():
        target = root / "uploads" / f"{int(time.time())}-{filename}"

    max_bytes = config.max_upload_mb * 1024 * 1024
    size = 0
    digest = hashlib.sha256()
    with target.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="File is too large")
            digest.update(chunk)
            handle.write(chunk)

    rel_path = _rel_under_session(session_id, target)
    store.add_file(session_id, "upload", target.name, rel_path, size, digest.hexdigest(), now_ts())
    store.add_message(session_id, "system", f"Uploaded file: {rel_path}", now_ts())
    store.audit("upload", ctx["user_label"], rel_path, now_ts())
    return RedirectResponse(f"/chat/{session_id}", status_code=303)


@app.get("/chat/{session_id}/files.json")
def list_session_files(request: Request, session_id: str) -> JSONResponse:
    ctx = current_context(request, require_approved=True)
    _require_visible_session(session_id, ctx)
    _register_outputs(session_id)
    rows = store.list_files(session_id)
    payload = [
        {
            "id": int(row["id"]),
            "filename": row["filename"],
            "kind": row["kind"],
            "size": int(row["size"]),
        }
        for row in rows
    ]
    return JSONResponse(payload)


@app.get("/chat/{session_id}/messages.json")
def list_session_messages(request: Request, session_id: str) -> JSONResponse:
    ctx = current_context(request, require_approved=True)
    session = _require_visible_session(session_id, ctx)
    _register_outputs(session_id)
    rows = store.list_messages(session_id)
    file_rows = store.list_files(session_id)
    return JSONResponse(
        {
            "session": {
                "id": session_id,
                "status": session["status"],
                "running": _active_process(session_id) is not None,
            },
            "messages": [
                {
                    "id": int(row["id"]),
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": int(row["created_at"]),
                }
                for row in rows
            ],
            "files": [
                {
                    "id": int(row["id"]),
                    "filename": row["filename"],
                    "kind": row["kind"],
                    "size": int(row["size"]),
                }
                for row in file_rows
            ],
        }
    )


@app.get("/files/{file_id}/download")
def download_file(request: Request, file_id: int) -> FileResponse:
    ctx = current_context(request, require_approved=True)
    row = store.file_by_id(file_id)
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    _require_visible_session(str(row["session_id"]), ctx)
    path = _session_root(str(row["session_id"])) / str(row["rel_path"])
    _rel_under_session(str(row["session_id"]), path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File missing")
    return FileResponse(path, filename=str(row["filename"]))


@app.post("/chat/{session_id}/terminate")
def terminate_chat(request: Request, session_id: str) -> RedirectResponse:
    ctx = current_context(request, require_approved=True)
    _require_session_for_device(session_id, ctx)
    proc = _active_process(session_id)
    if proc:
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            _write_session_recovery(
                session_id,
                "force-killed",
                "# Hermes force-kill recovery\n\n"
                "The background agent process did not exit after SIGTERM and was killed by the web server.",
            )
            proc.kill()
            proc.join(timeout=2)
        else:
            _write_session_recovery(
                session_id,
                "terminated",
                "# Hermes termination recovery\n\n"
                "The user manually terminated this session. Any partial files already written by the agent remain in work/ or outputs/.",
            )
        _session_processes.pop(session_id, None)
    store.close_session(session_id, now_ts())
    store.add_message(session_id, "system", "Session terminated by user.", now_ts())
    _register_outputs(session_id)
    store.audit("session_terminated", ctx["user_label"], session_id, now_ts())
    return RedirectResponse(f"/chat/{session_id}", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request) -> HTMLResponse:
    ctx = current_context(request)
    if not ctx["admin"]:
        raise HTTPException(status_code=403, detail="Admin only")
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "config": config,
            "ctx": ctx,
            "admin": True,
            "devices": store.list_devices(),
            "audit": store.list_audit(),
        },
    )


@app.get("/admin/config", response_class=HTMLResponse)
def admin_config(request: Request) -> HTMLResponse:
    ctx = current_context(request)
    if not ctx["admin"]:
        raise HTTPException(status_code=403, detail="Admin only")
    unified = load_unified_config()
    fields = []
    for dotted, label, description, input_type in FIELD_SPECS:
        value = get_path(unified, dotted, "")
        fields.append(
            {
                "name": dotted,
                "label": label,
                "description": description,
                "type": input_type,
                "value": value,
                "display_value": masked(value) if dotted in SECRET_FIELDS else value,
                "secret": dotted in SECRET_FIELDS,
            }
        )
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "config": config,
            "ctx": ctx,
            "admin": True,
            "fields": fields,
            "providers_yaml": yaml.safe_dump(get_path(unified, "llm.providers", {}), sort_keys=False, allow_unicode=True),
        },
    )


@app.post("/admin/config")
async def admin_config_save(request: Request) -> RedirectResponse:
    ctx = current_context(request)
    if not ctx["admin"]:
        raise HTTPException(status_code=403, detail="Admin only")
    form = await request.form()
    unified = load_unified_config()
    checkbox_fields = {name for name, _, _, input_type in FIELD_SPECS if input_type == "checkbox"}
    number_fields = {name for name, _, _, input_type in FIELD_SPECS if input_type == "number"}

    for dotted, _label, _description, input_type in FIELD_SPECS:
        if dotted in checkbox_fields:
            set_path(unified, dotted, dotted in form)
            continue
        raw = str(form.get(dotted, "")).strip()
        # A blank secret means "leave the stored one alone", so the page can be saved
        # without re-typing every key. A blank number means the same, which also keeps
        # a partial form submission from rejecting the whole save.
        if raw == "" and (dotted in SECRET_FIELDS or dotted in number_fields):
            continue
        if dotted in number_fields:
            try:
                set_path(unified, dotted, int(raw))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid number for {dotted}") from None
        else:
            set_path(unified, dotted, raw)
    providers_raw = str(form.get("llm.providers.__yaml", "")).strip()
    if providers_raw:
        try:
            providers = yaml.safe_load(providers_raw) or {}
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid provider YAML: {exc}") from None
        if not isinstance(providers, dict):
            raise HTTPException(status_code=400, detail="llm.providers must be a YAML mapping")
        set_path(unified, "llm.providers", providers)
    save_unified_config(unified)
    store.audit("config_saved", ctx["user_label"], "config/hermes.yaml", now_ts())
    return RedirectResponse("/admin/config?saved=1", status_code=303)


@app.post("/admin/devices/{device_id}/{action}")
def admin_device_action(request: Request, device_id: int, action: str) -> RedirectResponse:
    ctx = current_context(request)
    if not ctx["admin"]:
        raise HTTPException(status_code=403, detail="Admin only")
    if action not in {"approve", "reject", "revoke"}:
        raise HTTPException(status_code=400, detail="Invalid action")
    status = "approved" if action == "approve" else "rejected"
    store.set_device_status(device_id, status, now_ts() if status == "approved" else None)
    store.audit(f"device_{action}", ctx["user_label"], str(device_id), now_ts())
    return RedirectResponse("/admin", status_code=303)


@app.post("/logout")
def logout() -> RedirectResponse:
    redirect = RedirectResponse("/", status_code=303)
    redirect.delete_cookie(config.session_cookie_name)
    redirect.delete_cookie(config.device_cookie_name)
    return redirect


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
