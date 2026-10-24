"""Constrained query execution.

Responsibilities, kept separate from authorization:

* run only a :class:`ValidatedQuery` (the SQL serialized from a validated AST),
* on a dedicated, read-only connection with its own authorizer,
* under a wall-clock deadline enforced by SQLite's progress handler,
* returning at most ``max_rows`` rows and ``max_response_bytes`` of data,
* translating database errors into safe, generic error codes.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.database.authorizer import install_authorizer
from app.database.connection import DatabaseUnavailable, open_readonly_connection
from app.observability import metrics
from app.security.policies import AccessPolicy
from app.security.validated import ValidatedQuery

log = logging.getLogger(__name__)

SqlParam = str | int | float | None


class ExecutionErrorCode:
    TIMEOUT = "QUERY_TIMEOUT"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    EXECUTION_FAILED = "EXECUTION_FAILED"


_PUBLIC_MESSAGES = {
    ExecutionErrorCode.TIMEOUT: "the query exceeded the execution time limit",
    ExecutionErrorCode.AUTHORIZATION_DENIED: "the database refused access required by this query",
    ExecutionErrorCode.DATABASE_UNAVAILABLE: "the database is temporarily unavailable",
    ExecutionErrorCode.EXECUTION_FAILED: "the query failed during execution",
}


class ExecutionError(Exception):
    """Carries only a code and a fixed public message; never the raw DB error."""

    def __init__(self, code: str, request_id: str) -> None:
        self.code = code
        self.public_message = _PUBLIC_MESSAGES[code]
        self.request_id = request_id
        super().__init__(f"{code}: {self.public_message}")


@dataclass(frozen=True)
class ExecutionLimits:
    max_rows: int = 100
    max_response_bytes: int = 1_000_000
    timeout_ms: int = 2_000
    progress_ops: int = 1_000


@dataclass(frozen=True)
class QueryResult:
    request_id: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    duration_ms: float
    sql: str = field(repr=False, default="")
    plan: list[str] = field(default_factory=list)  # EXPLAIN QUERY PLAN details (index use, scans)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value[:1024]).decode("ascii")}
    return str(value)


def _classify(exc: sqlite3.Error, timed_out: bool, denied: bool) -> str:
    """Map a DB error to a public code. Uses our own state, not message parsing, first."""
    if timed_out:
        return ExecutionErrorCode.TIMEOUT
    if denied:
        return ExecutionErrorCode.AUTHORIZATION_DENIED
    message = str(exc).lower()
    if "interrupt" in message:
        return ExecutionErrorCode.TIMEOUT
    if "not authorized" in message or "prohibited" in message:
        return ExecutionErrorCode.AUTHORIZATION_DENIED
    return ExecutionErrorCode.EXECUTION_FAILED


class QueryExecutor:
    """Thread-safe: holds only immutable configuration. All per-query state is local."""

    def __init__(self, db_path: Path, policy: AccessPolicy, limits: ExecutionLimits) -> None:
        self._db_path = Path(db_path)
        self._policy = policy
        self._limits = limits

    def execute(
        self,
        query: ValidatedQuery,
        params: Sequence[SqlParam] = (),
        request_id: str | None = None,
    ) -> QueryResult:
        if not isinstance(query, ValidatedQuery):
            raise TypeError("QueryExecutor only executes ValidatedQuery objects")
        request_id = request_id or uuid.uuid4().hex
        limits = self._limits
        row_cap = min(query.effective_limit, limits.max_rows)

        try:
            conn = open_readonly_connection(self._db_path)
        except DatabaseUnavailable:
            metrics.QUERY_ERRORS.inc(code=ExecutionErrorCode.DATABASE_UNAVAILABLE)
            raise ExecutionError(ExecutionErrorCode.DATABASE_UNAVAILABLE, request_id) from None

        started = time.monotonic()
        deadline = started + limits.timeout_ms / 1000.0
        state = {"timed_out": False}
        audit = None

        def progress() -> int:
            if time.monotonic() > deadline:
                state["timed_out"] = True
                return 1  # non-zero aborts the statement
            return 0

        try:
            audit = install_authorizer(conn, self._policy)  # last setup step
            conn.set_progress_handler(progress, limits.progress_ops)
            cursor = conn.execute(query.sql, tuple(params))
            columns = [d[0] for d in (cursor.description or [])]

            rows: list[list[Any]] = []
            size = len(json.dumps(columns))
            truncated = False
            # Fetch one extra row to detect truncation without materializing more.
            for raw in cursor.fetchmany(row_cap + 1):
                if len(rows) >= row_cap:
                    truncated = True
                    break
                row = [_json_safe(v) for v in raw]
                size += len(json.dumps(row, default=str))
                if size > limits.max_response_bytes:
                    truncated = True
                    break
                rows.append(row)
            if state["timed_out"]:
                raise sqlite3.OperationalError("interrupted")
            plan = self._plan(conn, query.sql, params)
        except sqlite3.Error as exc:
            denials = list(audit.denials) if audit is not None else []
            code = _classify(exc, state["timed_out"], bool(denials))
            # Full detail stays in server logs, keyed by request id. Never returned.
            log.warning(
                "query failed request_id=%s code=%s error_type=%s denials=%s",
                request_id, code, type(exc).__name__, denials,
            )
            metrics.QUERY_ERRORS.inc(code=code)
            raise ExecutionError(code, request_id) from None
        finally:
            try:
                conn.set_progress_handler(None, 0)
            finally:
                conn.close()

        metrics.QUERY_LATENCY.observe(time.monotonic() - started)
        return QueryResult(
            request_id=request_id,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            sql=query.sql,
            plan=plan,
        )

    @staticmethod
    def _plan(conn: sqlite3.Connection, sql: str, params: Sequence[SqlParam]) -> list[str]:
        """Best-effort query plan for transparency (same connection, same authorizer)."""
        try:
            rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", tuple(params)).fetchmany(30)
        except sqlite3.Error:
            return []
        return [str(r[3])[:200] for r in rows if len(r) > 3]
