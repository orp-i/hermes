from __future__ import annotations

import os
from typing import Any

from ..sandbox import docker_exec, session_workdir


MAX_BASH_TIMEOUT = int(os.getenv("HERMES_MAX_TOOL_TIMEOUT_SEC", "1200"))

SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Execute a bash command inside the persistent Hermes sandbox container. "
            "The sandbox keeps state across calls (installed pip packages, files, cwd within work/). "
            "Default working directory is the current session's work/ folder. "
            "Read-only host code is mounted at /code (read it but don't modify). "
            "Session uploads are at ../uploads/ (relative to cwd) and downloadable outputs go in ../outputs/."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Bash command to run. Pipes, redirects, and multi-line scripts are supported.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120, max 1200).",
                    "minimum": 1,
                    "maximum": 1200,
                },
            },
            "required": ["command"],
        },
    },
}


def run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    command = str(args.get("command", "")).strip()
    if not command:
        return {"stdout": "", "stderr": "Empty command", "exit_code": 2, "duration_ms": 0}
    timeout = int(args.get("timeout") or 120)
    timeout = max(1, min(timeout, MAX_BASH_TIMEOUT))
    return docker_exec(command, workdir=session_workdir(session_id), timeout=timeout)
