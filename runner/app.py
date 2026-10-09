"""Hermes sandbox runner.

A small FastAPI service that owns /var/run/docker.sock and proxies docker exec
calls into the persistent sandbox container. The agent container talks to this
service over HTTP, which keeps docker.sock out of the agent's blast radius.

Endpoints:
- GET  /healthz          — liveness, no auth
- POST /exec             — run a command inside the sandbox
- POST /ensure-session-dirs — create per-session subdirectories
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import shlex
import time

import docker
from docker.errors import APIError, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SANDBOX_CONTAINER = os.getenv("HERMES_SANDBOX_CONTAINER", "hermes-sandbox")
WORKSPACE_ROOT = os.getenv("HERMES_SANDBOX_WORKSPACE", "/workspace")
RUNNER_TOKEN = os.getenv("HERMES_RUNNER_TOKEN", "")
MAX_TOOL_TIMEOUT_SEC = int(os.getenv("HERMES_RUNNER_MAX_TOOL_TIMEOUT_SEC", "1200"))
SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")

if not RUNNER_TOKEN:
    raise RuntimeError("HERMES_RUNNER_TOKEN must be set for the runner service")

app = FastAPI(title="hermes-runner", version="0.2.0")
_client: docker.DockerClient | None = None


def _docker() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(token, RUNNER_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")


def _validate_workdir(workdir: str) -> None:
    root = os.path.normpath(WORKSPACE_ROOT)
    path = os.path.normpath(workdir)
    if path != root and not path.startswith(root + "/"):
        raise HTTPException(status_code=400, detail="workdir must be under workspace")


def _validate_session_id(session_id: str) -> None:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")


class ExecRequest(BaseModel):
    command: str
    workdir: str = "/"
    timeout: int = Field(default=120, ge=1, le=MAX_TOOL_TIMEOUT_SEC)
    env: dict[str, str] | None = None


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool


class EnsureSessionRequest(BaseModel):
    session_id: str


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/exec", response_model=ExecResponse)
def exec_endpoint(req: ExecRequest, _: None = Depends(require_token)) -> ExecResponse:
    _validate_workdir(req.workdir)
    safe_timeout = max(1, min(int(req.timeout), MAX_TOOL_TIMEOUT_SEC))
    wrapped = (
        f"timeout --signal=TERM --kill-after=2 {safe_timeout} "
        f"bash -lc {shlex.quote(req.command)}"
    )

    start = time.time()
    try:
        container = _docker().containers.get(SANDBOX_CONTAINER)
    except NotFound:
        raise HTTPException(
            status_code=503,
            detail=f"sandbox container {SANDBOX_CONTAINER!r} not running",
        )
    except APIError as exc:
        raise HTTPException(status_code=502, detail=f"docker API error: {exc}") from exc

    try:
        result = container.exec_run(
            cmd=["sh", "-c", wrapped],
            workdir=req.workdir,
            environment=req.env or None,
            demux=True,
            stdout=True,
            stderr=True,
        )
    except APIError as exc:
        raise HTTPException(status_code=502, detail=f"docker exec failed: {exc}") from exc

    exit_code = int(result.exit_code or 0)
    out_bytes, err_bytes = (
        result.output if isinstance(result.output, tuple) else (result.output, b"")
    )
    stdout = (out_bytes or b"").decode("utf-8", errors="replace")
    stderr = (err_bytes or b"").decode("utf-8", errors="replace")
    timed_out = exit_code == 124
    if timed_out:
        stderr = (stderr + f"\n[Timed out after {safe_timeout}s]").lstrip()
    return ExecResponse(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=int((time.time() - start) * 1000),
        timed_out=timed_out,
    )


@app.post("/ensure-session-dirs")
def ensure_session_dirs(req: EnsureSessionRequest, _: None = Depends(require_token)) -> dict[str, str]:
    _validate_session_id(req.session_id)
    workdir = f"{WORKSPACE_ROOT}/sessions/{req.session_id}"
    parts = [
        f"{workdir}/work",
        f"{workdir}/uploads",
        f"{workdir}/outputs",
        f"{workdir}/logs",
    ]
    cmd = "mkdir -p " + " ".join(shlex.quote(p) for p in parts)
    try:
        container = _docker().containers.get(SANDBOX_CONTAINER)
    except NotFound:
        raise HTTPException(status_code=503, detail="sandbox not running")
    result = container.exec_run(cmd=["sh", "-c", cmd], workdir="/")
    if result.exit_code != 0:
        raise HTTPException(
            status_code=500,
            detail=f"mkdir failed: {(result.output or b'').decode('utf-8', 'replace')[:500]}",
        )
    return {"status": "ok", "workdir": workdir}
