from __future__ import annotations

import shlex
from typing import Any

from ..sandbox import docker_exec, session_workdir
from .files import _resolve


LS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ls",
        "description": (
            "List files in the sandbox without modifying anything. Prefer this over bash for directory inspection. "
            "Relative paths are resolved against the current session work/ directory; absolute paths can inspect /code, uploads, or outputs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory or file path to list (default .)."},
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum entries to return (default 200, max 1000).",
                    "minimum": 1,
                    "maximum": 1000,
                },
                "show_hidden": {"type": "boolean", "description": "Include dotfiles (default true)."},
            },
        },
    },
}

GREP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search text files in the sandbox without modifying anything. Prefer this over bash for code/text search. "
            "Uses ripgrep when available and falls back to grep."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Literal or regex search pattern."},
                "path": {"type": "string", "description": "File or directory to search (default .)."},
                "max_matches": {
                    "type": "integer",
                    "description": "Maximum matching lines to return (default 200, max 1000).",
                    "minimum": 1,
                    "maximum": 1000,
                },
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default true)."},
                "glob": {"type": "string", "description": "Optional include glob, e.g. '*.py'."},
            },
            "required": ["query"],
        },
    },
}

FIND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find",
        "description": (
            "Find files or directories by name in the sandbox without modifying anything. Prefer this over bash for path discovery."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root path to search (default .)."},
                "name": {"type": "string", "description": "Name glob, e.g. '*.py' or 'README*' (default '*')."},
                "type": {
                    "type": "string",
                    "enum": ["file", "dir", "any"],
                    "description": "Restrict result type (default any).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum paths to return (default 200, max 1000).",
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
        },
    },
}


def _limit(value: Any, default: int, upper: int = 1000) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, upper))


def ls_run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    raw_path = str(args.get("path") or ".").strip() or "."
    max_entries = _limit(args.get("max_entries"), 200)
    show_hidden = bool(args.get("show_hidden", True))
    target = _resolve(raw_path, session_id)
    list_flag = "-la" if show_hidden else "-l"
    cmd = (
        f"if [ ! -e {shlex.quote(target)} ]; then echo 'No such path' >&2; exit 1; fi; "
        f"if [ -d {shlex.quote(target)} ]; then "
        f"  ls {list_flag} {shlex.quote(target)} | sed -n '1,{max_entries + 1}p'; "
        f"else "
        f"  ls {list_flag} {shlex.quote(target)}; "
        f"fi"
    )
    return docker_exec(cmd, workdir=session_workdir(session_id), timeout=30)


def grep_run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"stdout": "", "stderr": "Missing query", "exit_code": 2, "duration_ms": 0}
    raw_path = str(args.get("path") or ".").strip() or "."
    max_matches = _limit(args.get("max_matches"), 200)
    case_sensitive = bool(args.get("case_sensitive", True))
    glob = str(args.get("glob") or "").strip()
    target = _resolve(raw_path, session_id)

    rg_flags = ["--line-number", "--color", "never", "--hidden", "--no-heading", "-m", str(max_matches)]
    grep_flags = ["-R", "-n", "-m", str(max_matches)]
    if not case_sensitive:
        rg_flags.append("-i")
        grep_flags.append("-i")
    if glob:
        rg_flags.extend(["-g", glob])
        grep_glob_note = "echo 'glob filter requires ripgrep; fallback grep will search all files' >&2; "
    else:
        grep_glob_note = ""

    rg_cmd = " ".join(["rg", *map(shlex.quote, rg_flags), shlex.quote(query), shlex.quote(target)])
    grep_cmd = " ".join(["grep", *map(shlex.quote, grep_flags), shlex.quote(query), shlex.quote(target)])
    cmd = (
        f"if [ ! -e {shlex.quote(target)} ]; then echo 'No such path' >&2; exit 1; fi; "
        f"if command -v rg >/dev/null 2>&1; then {rg_cmd}; "
        f"else {grep_glob_note}{grep_cmd}; fi"
    )
    return docker_exec(cmd, workdir=session_workdir(session_id), timeout=60)


def find_run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    raw_path = str(args.get("path") or ".").strip() or "."
    name = str(args.get("name") or "*").strip() or "*"
    kind = str(args.get("type") or "any").strip().lower()
    max_results = _limit(args.get("max_results"), 200)
    target = _resolve(raw_path, session_id)
    type_expr = ""
    if kind == "file":
        type_expr = " -type f"
    elif kind == "dir":
        type_expr = " -type d"
    cmd = (
        f"if [ ! -e {shlex.quote(target)} ]; then echo 'No such path' >&2; exit 1; fi; "
        f"find {shlex.quote(target)}{type_expr} -name {shlex.quote(name)} "
        f"| sort | sed -n '1,{max_results}p'"
    )
    return docker_exec(cmd, workdir=session_workdir(session_id), timeout=30)
