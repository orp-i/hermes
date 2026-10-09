from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    model TEXT,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_session
    ON agent_messages(session_id, ts);

CREATE TABLE IF NOT EXISTS agent_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    tool TEXT NOT NULL,
    input TEXT NOT NULL,
    stdout TEXT,
    stderr TEXT,
    exit_code INTEGER,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_executions_session
    ON agent_executions(session_id, ts);

CREATE TABLE IF NOT EXISTS agent_context_compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    compacted INTEGER NOT NULL,
    tokens_before INTEGER,
    tokens_after INTEGER,
    usable_tokens INTEGER,
    old_messages INTEGER,
    kept_recent_messages INTEGER,
    summary_chars INTEGER,
    summary TEXT,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_context_compactions_session
    ON agent_context_compactions(session_id, ts);

CREATE TABLE IF NOT EXISTS agent_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    iteration INTEGER,
    route TEXT NOT NULL,
    cached INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    tokens_estimate INTEGER,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_routes_session
    ON agent_routes(session_id, ts);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "agent_executions", "tool_call_id", "tool_call_id TEXT")
        _ensure_column(conn, "agent_executions", "route", "route TEXT")
        _ensure_column(conn, "agent_executions", "iteration", "iteration INTEGER")
        _ensure_column(conn, "agent_executions", "execution_mode", "execution_mode TEXT")
        _ensure_column(conn, "agent_executions", "readonly", "readonly INTEGER")
        _ensure_column(conn, "agent_executions", "blocked_by_policy", "blocked_by_policy INTEGER")
        _ensure_column(conn, "agent_executions", "policy_reason", "policy_reason TEXT")
        _ensure_column(conn, "agent_executions", "timed_out", "timed_out INTEGER")


def log_message(
    db_path: Path,
    session_id: str,
    role: str,
    *,
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    tool_call_id: str | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    with _lock, sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO agent_messages
                (session_id, ts, role, model, content, tool_calls,
                 tool_call_id, prompt_tokens, completion_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                time.time(),
                role,
                model,
                content,
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                tool_call_id,
                prompt_tokens,
                completion_tokens,
            ),
        )


def log_execution(
    db_path: Path,
    session_id: str,
    tool: str,
    input_data: dict[str, Any],
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    duration_ms: int = 0,
    tool_call_id: str | None = None,
    route: str | None = None,
    iteration: int | None = None,
    execution_mode: str | None = None,
    readonly: bool | None = None,
    blocked_by_policy: bool = False,
    policy_reason: str | None = None,
    timed_out: bool = False,
) -> None:
    with _lock, sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO agent_executions
                (session_id, ts, tool, input, stdout, stderr,
                 exit_code, duration_ms, tool_call_id, route, iteration,
                 execution_mode, readonly, blocked_by_policy, policy_reason,
                 timed_out)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                time.time(),
                tool,
                json.dumps(input_data, ensure_ascii=False),
                stdout,
                stderr,
                exit_code,
                duration_ms,
                tool_call_id,
                route,
                iteration,
                execution_mode,
                None if readonly is None else int(readonly),
                int(blocked_by_policy),
                policy_reason,
                int(timed_out),
            ),
        )


def log_context_compaction(
    db_path: Path,
    session_id: str,
    metadata: dict[str, Any],
    *,
    summary: str | None = None,
) -> None:
    with _lock, sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO agent_context_compactions
                (session_id, ts, compacted, tokens_before, tokens_after,
                 usable_tokens, old_messages, kept_recent_messages,
                 summary_chars, summary, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                time.time(),
                int(bool(metadata.get("compacted"))),
                metadata.get("tokens_before"),
                metadata.get("tokens_after"),
                metadata.get("usable_tokens"),
                metadata.get("old_messages"),
                metadata.get("kept_recent_messages"),
                metadata.get("summary_chars"),
                summary,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )


def log_route(
    db_path: Path,
    session_id: str,
    *,
    route: str,
    iteration: int | None = None,
    cached: bool = False,
    reason: str | None = None,
    tokens_estimate: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    with _lock, sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO agent_routes
                (session_id, ts, iteration, route, cached, reason,
                 tokens_estimate, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                time.time(),
                iteration,
                route,
                int(cached),
                reason,
                tokens_estimate,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )


def list_session_messages(db_path: Path, session_id: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM agent_messages WHERE session_id = ? ORDER BY ts ASC, id ASC",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_session_executions(db_path: Path, session_id: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM agent_executions WHERE session_id = ? ORDER BY ts ASC, id ASC",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]
