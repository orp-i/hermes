from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .tools import TOOL_SPECS


@dataclass(frozen=True)
class PreparedToolCall:
    index: int
    id: str
    name: str
    args: dict[str, Any]
    pre_result: dict[str, Any] | None = None


def tool_execution_mode(name: str) -> str:
    spec = TOOL_SPECS.get(name)
    return spec.execution_mode if spec else "sequential"


def parse_tool_call(index: int, tool_call: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    tc_id = tool_call.get("id") or ""
    name = tool_call.get("function", {}).get("name") or ""
    raw_args = tool_call.get("function", {}).get("arguments") or "{}"
    try:
        args: dict[str, Any] = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        args = {"_parse_error": str(exc), "_raw": raw_args}
    return str(tc_id), str(name), args


def before_tool_call(name: str, args: dict[str, Any], *, session_id: str) -> dict[str, Any] | None:
    """Preflight hook for policy checks before a tool handler runs."""
    if name in {"write_file", "edit_file"}:
        target = str(args.get("path") or "").strip()
        if target == "/code" or target.startswith("/code/"):
            return {
                "stdout": "",
                "stderr": f"Tool execution blocked: {name} cannot write to /code",
                "exit_code": 1,
                "duration_ms": 0,
            }
    return None


def after_tool_call(
    name: str,
    args: dict[str, Any],
    result: dict[str, Any],
    *,
    session_id: str,
) -> dict[str, Any]:
    """Postprocess hook for cross-cutting tool result behavior."""
    if name in {"ls", "grep", "find"} and result.get("exit_code") == 0 and not result.get("stdout"):
        return {**result, "stdout": "(no results)"}
    return result


def can_execute_parallel(calls: list[PreparedToolCall]) -> bool:
    return len(calls) > 1 and all(tool_execution_mode(call.name) == "parallel_safe" for call in calls)
