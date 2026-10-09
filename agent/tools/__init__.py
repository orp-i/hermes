from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import bash, files, report_render, shell_readonly, validate_references, web_search


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    schema: dict
    handler: Callable[..., dict[str, Any]]
    execution_mode: str = "sequential"
    readonly: bool = False

TOOL_SCHEMAS: list[dict] = [
    bash.SCHEMA,
    files.READ_SCHEMA,
    files.WRITE_SCHEMA,
    files.EDIT_SCHEMA,
    shell_readonly.LS_SCHEMA,
    shell_readonly.GREP_SCHEMA,
    shell_readonly.FIND_SCHEMA,
    report_render.SCHEMA,
    report_render.REVIEW_SCHEMA,
    web_search.SCHEMA,
    validate_references.SCHEMA,
]

TOOL_SPECS = {
    "bash": ToolSpec(bash.SCHEMA, bash.run),
    "read_file": ToolSpec(files.READ_SCHEMA, files.read_run, execution_mode="parallel_safe", readonly=True),
    "write_file": ToolSpec(files.WRITE_SCHEMA, files.write_run),
    "edit_file": ToolSpec(files.EDIT_SCHEMA, files.edit_run),
    "ls": ToolSpec(shell_readonly.LS_SCHEMA, shell_readonly.ls_run, execution_mode="parallel_safe", readonly=True),
    "grep": ToolSpec(shell_readonly.GREP_SCHEMA, shell_readonly.grep_run, execution_mode="parallel_safe", readonly=True),
    "find": ToolSpec(shell_readonly.FIND_SCHEMA, shell_readonly.find_run, execution_mode="parallel_safe", readonly=True),
    "render_report": ToolSpec(report_render.SCHEMA, report_render.run),
    "review_report_layout": ToolSpec(report_render.REVIEW_SCHEMA, report_render.review_run),
    "web_search": ToolSpec(web_search.SCHEMA, web_search.run, execution_mode="parallel_safe", readonly=True),
    "validate_references": ToolSpec(
        validate_references.SCHEMA,
        validate_references.run,
        execution_mode="parallel_safe",
        readonly=True,
    ),
}

DISPATCH = {name: spec.handler for name, spec in TOOL_SPECS.items()}
