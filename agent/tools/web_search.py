from __future__ import annotations

import time
import os
from pathlib import Path
from typing import Any

from ..sandbox import SANDBOX_WORKSPACE_ROOT
from ..search import MultiBackend, get_backend, _search_with_timeout

SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Returns a list of {title, url, snippet}. "
            "Use this when the user asks about recent events, current API documentation, "
            "or anything that might have changed since model training. "
            "The current date is provided in the system prompt — when the user asks for "
            "'latest' / 'today' / 'this week' content, include the current year (and month "
            "if appropriate) in the query so results are not stale. Search calls are budgeted; "
            "refine queries carefully and synthesize from existing results instead of repeating broad searches."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 10).",
                    "minimum": 1,
                    "maximum": 10,
                },
                "time_range": {
                    "type": "string",
                    "description": "Optional Tavily recency filter: day, week, month, year, or d/w/m/y.",
                },
                "topic": {
                    "type": "string",
                    "description": "Optional Tavily topic, such as general or news.",
                },
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domains to include, e.g. ['openai.com'].",
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional domains to exclude.",
                },
                "include_raw_content": {
                    "type": "boolean",
                    "description": "Request cleaned page text from supported backends for deeper analysis.",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Maximum seconds for the whole search tool call (default 18, max 45).",
                    "minimum": 3,
                    "maximum": 45,
                },
            },
            "required": ["query"],
        },
    },
}


def _session_outputs_dir(session_id: str) -> Path:
    path = Path(SANDBOX_WORKSPACE_ROOT) / "sessions" / session_id / "outputs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_search_diagnostic(session_id: str, query: str, message: str) -> str:
    ts = int(time.time())
    path = _session_outputs_dir(session_id) / f"web-search-diagnostic-{ts}.md"
    path.write_text(
        "# Web search diagnostic\n\n"
        f"- Query: `{query}`\n"
        f"- Message: {message}\n\n"
        "The search tool returned a diagnostic instead of silently failing. "
        "Retry with a narrower query, fewer results, or specific include_domains if needed.\n",
        encoding="utf-8",
    )
    return f"outputs/{path.name}"


def run(args: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"stdout": "", "stderr": "Empty query", "exit_code": 2, "duration_ms": 0}
    max_results = int(args.get("max_results") or 5)
    max_results = max(1, min(max_results, 10))
    try:
        timeout_sec = int(args.get("timeout_sec") or os.getenv("HERMES_SEARCH_TOOL_TIMEOUT_SEC", "18"))
    except (TypeError, ValueError):
        timeout_sec = 18
    timeout_sec = max(3, min(timeout_sec, 45))

    start = time.time()
    try:
        backend = get_backend()
        kwargs = {
            "time_range": args.get("time_range"),
            "topic": args.get("topic"),
            "include_domains": args.get("include_domains"),
            "exclude_domains": args.get("exclude_domains"),
            "include_raw_content": bool(args.get("include_raw_content", False)),
        }
        if isinstance(backend, MultiBackend):
            kwargs["per_backend_timeout"] = max(2, min(timeout_sec / max(1, len(backend.backends)), timeout_sec))
            results = backend.search(query, max_results=max_results, **kwargs)
        else:
            results, error = _search_with_timeout(
                backend,
                query,
                max_results=max_results,
                timeout=timeout_sec,
                **kwargs,
            )
            if error:
                raise RuntimeError(error)
            results = results or []
    except Exception as exc:
        diagnostic = _write_search_diagnostic(session_id, query, f"Search failed: {exc}")
        return {
            "stdout": (
                f"Search did not complete successfully.\n"
                f"Diagnostic saved: {diagnostic}\n"
                f"Reason: {exc}\n"
                "Use a narrower query, reduce max_results, set include_domains, or retry later."
            ),
            "stderr": f"Search failed: {exc}",
            "exit_code": 1,
            "duration_ms": int((time.time() - start) * 1000),
            "timed_out": "timed out" in str(exc).lower(),
        }
    duration_ms = int((time.time() - start) * 1000)

    if not results:
        return {"stdout": "(no results)", "stderr": "", "exit_code": 0, "duration_ms": duration_ms}

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    {r['url']}")
        if r.get("snippet"):
            lines.append(f"    {r['snippet']}")
        if r.get("raw_content"):
            raw = str(r["raw_content"])
            lines.append(f"    raw_content: {raw[:2000]}")
        images = r.get("images") or []
        if images:
            lines.append("    images:")
            for url in images[:5]:
                lines.append(f"      - {url}")
        lines.append("")
    return {"stdout": "\n".join(lines).strip(), "stderr": "", "exit_code": 0, "duration_ms": duration_ms}
