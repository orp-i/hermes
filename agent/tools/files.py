from __future__ import annotations

import shlex
import json
from typing import Any

from ..sandbox import docker_exec, session_workdir

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a text file from the sandbox. Path is resolved against the current "
            "session's work/ dir unless absolute. Use absolute paths to read /code (the "
            "read-only mount of the host hermes project) or /workspace/sessions/<id>/uploads/."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to work/ or absolute)."},
                "max_bytes": {
                    "type": "integer",
                    "description": "Truncate output to this many bytes (default 65536).",
                    "minimum": 256,
                    "maximum": 1048576,
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional 1-based first line to read. Use with end_line for focused reads.",
                    "minimum": 1,
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional 1-based final line to read. Requires start_line; capped to 5000 lines.",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        },
    },
}

WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write text to a file in the sandbox. Relative paths land in the session's work/ "
            "or outputs/ dir. To make a file user-downloadable, write to outputs/<name>. "
            "Refuses to write to /code (read-only host mount)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to work/ or absolute)."},
                "content": {"type": "string", "description": "Full file contents (UTF-8)."},
                "append": {"type": "boolean", "description": "Append instead of overwrite (default false)."},
            },
            "required": ["path", "content"],
        },
    },
}

EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Edit a sandbox text file by replacing exact old_text with new_text. "
            "Use this for targeted edits instead of rewriting an entire file. "
            "Refuses to write to /code and fails unless old_text appears exactly once by default."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to work/ or absolute)."},
                "old_text": {"type": "string", "description": "Exact text to replace."},
                "new_text": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all matches instead of requiring exactly one match (default false).",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
}


def _resolve(path: str, session_id: str) -> str:
    if path.startswith("/"):
        return path
    return f"{session_workdir(session_id)}/{path}"


def read_run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    raw_path = str(args.get("path", "")).strip()
    if not raw_path:
        return {"stdout": "", "stderr": "Missing path", "exit_code": 2, "duration_ms": 0}
    max_bytes = int(args.get("max_bytes") or 65536)
    max_bytes = max(256, min(max_bytes, 1048576))
    target = _resolve(raw_path, session_id)
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    if start_line is not None:
        try:
            start = max(1, int(start_line))
            end = int(end_line or start + 499)
        except (TypeError, ValueError):
            return {"stdout": "", "stderr": "Invalid start_line/end_line", "exit_code": 2, "duration_ms": 0}
        end = max(start, min(end, start + 4999))
        cmd = (
            f"if [ ! -e {shlex.quote(target)} ]; then echo 'No such file' >&2; exit 1; fi; "
            f"sed -n '{start},{end}p' {shlex.quote(target)} | head -c {max_bytes}; "
            f"echo; echo \"[lines {start}-{end}, max_bytes={max_bytes}]\""
        )
        return docker_exec(cmd, workdir=session_workdir(session_id), timeout=30)
    cmd = (
        f"if [ ! -e {shlex.quote(target)} ]; then echo 'No such file' >&2; exit 1; fi; "
        f"head -c {max_bytes} {shlex.quote(target)}; "
        f"size=$(stat -c%s {shlex.quote(target)} 2>/dev/null || stat -f%z {shlex.quote(target)}); "
        f"if [ \"$size\" -gt {max_bytes} ]; then "
        f"  echo; echo \"...[truncated, file is $size bytes]\"; "
        f"fi"
    )
    return docker_exec(cmd, workdir=session_workdir(session_id), timeout=30)


def write_run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    raw_path = str(args.get("path", "")).strip()
    content = args.get("content", "")
    append = bool(args.get("append", False))
    if not raw_path:
        return {"stdout": "", "stderr": "Missing path", "exit_code": 2, "duration_ms": 0}
    target = _resolve(raw_path, session_id)
    if target.startswith("/code/") or target == "/code":
        return {"stdout": "", "stderr": "Refusing to write to /code (read-only host mount)", "exit_code": 1, "duration_ms": 0}

    op = ">>" if append else ">"
    heredoc_tag = "HERMES_EOF_" + str(abs(hash(content)) % (10**8))
    cmd = (
        f"mkdir -p \"$(dirname {shlex.quote(target)})\" && "
        f"cat <<'{heredoc_tag}' {op} {shlex.quote(target)}\n"
        f"{content}\n"
        f"{heredoc_tag}\n"
        f"echo wrote $(stat -c%s {shlex.quote(target)} 2>/dev/null || stat -f%z {shlex.quote(target)}) bytes to {target}"
    )
    return docker_exec(cmd, workdir=session_workdir(session_id), timeout=30)


def edit_run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    raw_path = str(args.get("path", "")).strip()
    old_text = args.get("old_text")
    new_text = args.get("new_text")
    replace_all = bool(args.get("replace_all", False))
    if not raw_path:
        return {"stdout": "", "stderr": "Missing path", "exit_code": 2, "duration_ms": 0}
    if not isinstance(old_text, str) or old_text == "":
        return {"stdout": "", "stderr": "old_text must be a non-empty string", "exit_code": 2, "duration_ms": 0}
    if not isinstance(new_text, str):
        return {"stdout": "", "stderr": "new_text must be a string", "exit_code": 2, "duration_ms": 0}
    target = _resolve(raw_path, session_id)
    if target.startswith("/code/") or target == "/code":
        return {"stdout": "", "stderr": "Refusing to edit /code (read-only host mount)", "exit_code": 1, "duration_ms": 0}

    payload = json.dumps(
        {
            "path": target,
            "old_text": old_text,
            "new_text": new_text,
            "replace_all": replace_all,
        },
        ensure_ascii=False,
    )
    script = r"""
import json
import os
from pathlib import Path

payload = json.loads(os.environ["HERMES_EDIT_JSON"])
path = Path(payload["path"])
if not path.exists():
    raise SystemExit("No such file")
text = path.read_text(encoding="utf-8")
old = payload["old_text"]
new = payload["new_text"]
count = text.count(old)
if count == 0:
    raise SystemExit("old_text not found")
if count > 1 and not payload.get("replace_all"):
    raise SystemExit(f"old_text matched {count} times; set replace_all=true or provide a unique snippet")
updated = text.replace(old, new) if payload.get("replace_all") else text.replace(old, new, 1)
path.write_text(updated, encoding="utf-8")
print(f"edited {path}: replaced {count if payload.get('replace_all') else 1} match(es), bytes={path.stat().st_size}")
"""
    cmd = f"python - <<'PY'\n{script}\nPY"
    return docker_exec(
        cmd,
        workdir=session_workdir(session_id),
        timeout=30,
        env={"HERMES_EDIT_JSON": payload},
    )
