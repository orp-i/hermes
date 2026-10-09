"""Sandbox client.

Talks HTTP to the hermes-runner service which holds /var/run/docker.sock.
The agent container itself has no docker access.
"""
from __future__ import annotations

import os

import httpx

RUNNER_BASE_URL = os.getenv("HERMES_RUNNER_BASE_URL", "http://hermes-runner:8650")
RUNNER_TOKEN = os.getenv("HERMES_RUNNER_TOKEN", "")
RUNNER_HTTP_TIMEOUT = float(os.getenv("HERMES_RUNNER_HTTP_TIMEOUT_SEC", "1220"))
MAX_TOOL_TIMEOUT_SEC = int(os.getenv("HERMES_MAX_TOOL_TIMEOUT_SEC", "1200"))
SANDBOX_WORKSPACE_ROOT = os.getenv("HERMES_SANDBOX_WORKSPACE", "/workspace")


def session_workdir(session_id: str) -> str:
    return f"{SANDBOX_WORKSPACE_ROOT}/sessions/{session_id}/work"


def _headers() -> dict[str, str]:
    if not RUNNER_TOKEN:
        raise RuntimeError("HERMES_RUNNER_TOKEN not set in agent container")
    return {"Authorization": f"Bearer {RUNNER_TOKEN}"}


def docker_exec(
    cmd: str,
    *,
    workdir: str,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> dict:
    """Run *cmd* in the sandbox via the runner. Returns stdout/stderr/exit_code/duration_ms/timed_out."""
    safe_timeout = max(1, min(int(timeout), MAX_TOOL_TIMEOUT_SEC))
    payload = {"command": cmd, "workdir": workdir, "timeout": safe_timeout}
    if env:
        payload["env"] = env
    try:
        with httpx.Client(timeout=RUNNER_HTTP_TIMEOUT) as client:
            resp = client.post(
                f"{RUNNER_BASE_URL}/exec",
                json=payload,
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        return {
            "stdout": "",
            "stderr": f"runner HTTP timeout after {RUNNER_HTTP_TIMEOUT}s",
            "exit_code": 124,
            "duration_ms": int(RUNNER_HTTP_TIMEOUT * 1000),
            "timed_out": True,
        }
    except httpx.HTTPStatusError as exc:
        return {
            "stdout": "",
            "stderr": f"runner returned {exc.response.status_code}: {exc.response.text[:500]}",
            "exit_code": 1,
            "duration_ms": 0,
            "timed_out": False,
        }
    except httpx.HTTPError as exc:
        return {
            "stdout": "",
            "stderr": f"runner HTTP error: {exc}",
            "exit_code": 1,
            "duration_ms": 0,
            "timed_out": False,
        }


def ensure_session_dirs(session_id: str) -> None:
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{RUNNER_BASE_URL}/ensure-session-dirs",
                json={"session_id": session_id},
                headers=_headers(),
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        # Best-effort: the bash tool will still surface a clear error if dirs
        # don't exist when the agent tries to use them.
        pass
