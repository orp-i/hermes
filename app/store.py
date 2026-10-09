from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists devices (
                  id integer primary key autoincrement,
                  token_hash text not null unique,
                  label text not null,
                  email text not null,
                  status text not null default 'pending',
                  created_at integer not null,
                  approved_at integer,
                  last_seen_at integer
                );

                create table if not exists audit_log (
                  id integer primary key autoincrement,
                  event text not null,
                  actor_email text not null,
                  target text not null,
                  created_at integer not null
                );

                create table if not exists sessions (
                  id text primary key,
                  device_id integer not null,
                  user_label text not null,
                  status text not null,
                  created_at integer not null,
                  updated_at integer not null,
                  expires_at integer not null
                );

                create table if not exists messages (
                  id integer primary key autoincrement,
                  session_id text not null,
                  role text not null,
                  content text not null,
                  created_at integer not null
                );

                create table if not exists session_files (
                  id integer primary key autoincrement,
                  session_id text not null,
                  kind text not null,
                  filename text not null,
                  rel_path text not null,
                  size integer not null,
                  sha256 text not null,
                  created_at integer not null
                );

                create index if not exists idx_devices_created_at
                  on devices(created_at desc);
                create index if not exists idx_audit_log_created_at
                  on audit_log(created_at desc);
                create index if not exists idx_sessions_status_expires
                  on sessions(status, expires_at);
                create index if not exists idx_sessions_device_status_created
                  on sessions(device_id, status, created_at desc);
                create index if not exists idx_sessions_user_status_created
                  on sessions(user_label, status, created_at desc);
                create index if not exists idx_messages_session_id
                  on messages(session_id, id);
                create index if not exists idx_session_files_session_created
                  on session_files(session_id, created_at desc);
                """
            )

    def device_by_hash(self, token_hash: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("select * from devices where token_hash = ?", (token_hash,)).fetchone()

    def create_device(self, token_hash: str, label: str, email: str, created_at: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert or ignore into devices (token_hash, label, email, created_at) values (?, ?, ?, ?)",
                (token_hash, label, email, created_at),
            )

    def update_last_seen(self, token_hash: str, ts: int) -> None:
        with self.connect() as conn:
            conn.execute("update devices set last_seen_at = ? where token_hash = ?", (ts, token_hash))

    def list_devices(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("select * from devices order by created_at desc"))

    def set_device_status(self, device_id: int, status: str, approved_at: int | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "update devices set status = ?, approved_at = ? where id = ?",
                (status, approved_at, device_id),
            )

    def audit(self, event: str, actor_email: str, target: str, created_at: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert into audit_log (event, actor_email, target, created_at) values (?, ?, ?, ?)",
                (event, actor_email, target, created_at),
            )

    def list_audit(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute("select * from audit_log order by created_at desc limit ?", (limit,))
            )

    def expire_sessions(self, ts: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "update sessions set status = 'expired' where status = 'active' and expires_at < ?",
                (ts,),
            )

    def active_session_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "select count(*) as n from sessions where status = 'active'"
            ).fetchone()
            return int(row["n"])

    def active_sessions_for_device(self, device_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "select * from sessions where status = 'active' and device_id = ? order by created_at desc",
                    (device_id,),
                )
            )

    def active_sessions_for_user(self, user_label: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "select * from sessions where status = 'active' and user_label = ? order by created_at desc",
                    (user_label,),
                )
            )

    def list_sessions_for_user(self, user_label: str, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    select
                      s.*,
                      (
                        select count(*)
                        from messages m
                        where m.session_id = s.id
                      ) as message_count,
                      (
                        select m.content
                        from messages m
                        where m.session_id = s.id and m.role = 'user'
                        order by m.id asc
                        limit 1
                      ) as title
                    from sessions s
                    where s.user_label = ?
                    order by s.updated_at desc, s.created_at desc
                    limit ?
                    """,
                    (user_label, limit),
                )
            )

    def create_session(
        self,
        session_id: str,
        device_id: int,
        user_label: str,
        created_at: int,
        expires_at: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into sessions (id, device_id, user_label, status, created_at, updated_at, expires_at)
                values (?, ?, ?, 'active', ?, ?, ?)
                """,
                (session_id, device_id, user_label, created_at, created_at, expires_at),
            )

    def session_by_id(self, session_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("select * from sessions where id = ?", (session_id,)).fetchone()

    def touch_session(self, session_id: str, updated_at: int, expires_at: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "update sessions set updated_at = ?, expires_at = ? where id = ?",
                (updated_at, expires_at, session_id),
            )

    def close_session(self, session_id: str, ts: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "update sessions set status = 'closed', updated_at = ? where id = ?",
                (ts, session_id),
            )

    def add_message(self, session_id: str, role: str, content: str, created_at: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert into messages (session_id, role, content, created_at) values (?, ?, ?, ?)",
                (session_id, role, content, created_at),
            )

    def list_messages(self, session_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "select * from messages where session_id = ? order by id asc",
                    (session_id,),
                )
            )

    def add_file(
        self,
        session_id: str,
        kind: str,
        filename: str,
        rel_path: str,
        size: int,
        sha256: str,
        created_at: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into session_files (session_id, kind, filename, rel_path, size, sha256, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, kind, filename, rel_path, size, sha256, created_at),
            )

    def list_files(self, session_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "select * from session_files where session_id = ? order by created_at desc",
                    (session_id,),
                )
            )

    def file_by_id(self, file_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("select * from session_files where id = ?", (file_id,)).fetchone()
