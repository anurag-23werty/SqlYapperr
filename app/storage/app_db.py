"""Application metadata store: users, sessions, datasets and the query log.

This database is trusted application state and is completely separate from the
users' data files. The LLM never sees it and generated SQL never runs on it.
All statements here are fixed and parameterised.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS datasets (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    db_file    TEXT NOT NULL,
    meta_json  TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_datasets_user ON datasets(user_id);
CREATE TABLE IF NOT EXISTS query_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    dataset_id      TEXT NOT NULL,
    conversation_id TEXT,
    question        TEXT NOT NULL,
    sql             TEXT,
    status          TEXT NOT NULL,
    codes           TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_conv ON query_log(user_id, dataset_id, conversation_id, created_at);
"""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    name: str
    created_at: float


@dataclass(frozen=True)
class SessionRecord:
    user: User
    csrf_token: str
    expires_at: float


@dataclass(frozen=True)
class DatasetRecord:
    id: str
    user_id: str
    name: str
    db_file: str
    meta: dict[str, Any]
    created_at: float


class AppDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ users
    def create_user(self, user_id: str, email: str, name: str, password_hash: str) -> User:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, name, password_hash, now),
            )
        return User(user_id, email, name, now)

    def user_credentials(self, email: str) -> tuple[User, str] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, email, name, created_at, password_hash FROM users WHERE email = ?", (email,)
            ).fetchone()
        return (User(row[0], row[1], row[2], row[3]), row[4]) if row else None

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))

    # --------------------------------------------------------------- sessions
    def create_session(self, token_hash: str, user_id: str, csrf_token: str, ttl_s: float) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (token_hash, user_id, csrf_token, now, now + ttl_s),
            )

    def get_session(self, token_hash: str) -> SessionRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.name, u.created_at, s.csrf_token, s.expires_at "
                "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                (token_hash,),
            ).fetchone()
        if not row or row[5] < time.time():
            return None
        return SessionRecord(User(row[0], row[1], row[2], row[3]), row[4], row[5])

    def delete_session(self, token_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # --------------------------------------------------------------- datasets
    def add_dataset(self, ds: DatasetRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO datasets (id, user_id, name, db_file, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ds.id, ds.user_id, ds.name, ds.db_file, json.dumps(ds.meta), ds.created_at),
            )

    def list_datasets(self, user_id: str) -> list[DatasetRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, user_id, name, db_file, meta_json, created_at FROM datasets "
                "WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [DatasetRecord(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5]) for r in rows]

    def get_dataset(self, user_id: str, dataset_id: str) -> DatasetRecord | None:
        """Ownership is part of the lookup: another user's id simply does not exist."""
        with self._conn() as conn:
            r = conn.execute(
                "SELECT id, user_id, name, db_file, meta_json, created_at FROM datasets WHERE id = ? AND user_id = ?",
                (dataset_id, user_id),
            ).fetchone()
        return DatasetRecord(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5]) if r else None

    def update_dataset_meta(self, user_id: str, dataset_id: str, meta: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE datasets SET meta_json = ? WHERE id = ? AND user_id = ?",
                (json.dumps(meta), dataset_id, user_id),
            )

    def delete_dataset(self, user_id: str, dataset_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM datasets WHERE id = ? AND user_id = ?", (dataset_id, user_id))
        return cur.rowcount > 0

    def count_datasets(self, user_id: str) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(1) FROM datasets WHERE user_id = ?", (user_id,)).fetchone()[0])

    # -------------------------------------------------------------- query log
    def log_query(
        self, *, request_id: str, user_id: str, dataset_id: str, conversation_id: str | None,
        question: str, sql: str | None, status: str, codes: list[str],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO query_log (request_id, user_id, dataset_id, conversation_id, question, sql, status, codes, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, user_id, dataset_id, conversation_id, question[:2000], sql, status,
                 json.dumps(codes[:20]), time.time()),
            )

    def recent_turns(self, user_id: str, dataset_id: str, conversation_id: str, limit: int) -> list[tuple[str, str]]:
        """Last successful (question, validated SQL) pairs for follow-up context."""
        if limit <= 0:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT question, sql FROM query_log WHERE user_id = ? AND dataset_id = ? AND conversation_id = ? "
                "AND status = 'ok' AND sql IS NOT NULL ORDER BY created_at DESC LIMIT ?",
                (user_id, dataset_id, conversation_id, limit),
            ).fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]
