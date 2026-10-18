"""Connection factory.

Every query gets its own short-lived connection. Nothing that carries security
state (the authorizer, the progress handler, SQLite limits) is shared between
requests, so one request can never disable or weaken another's controls.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Hard SQLite limits applied to every read-only connection (Python 3.11+).
_SQLITE_LIMITS: dict[int, int] = {
    sqlite3.SQLITE_LIMIT_ATTACHED: 0,          # ATTACH is impossible at the engine level
    sqlite3.SQLITE_LIMIT_SQL_LENGTH: 100_000,
    sqlite3.SQLITE_LIMIT_LENGTH: 1_000_000,    # max string/blob size
    sqlite3.SQLITE_LIMIT_COLUMN: 200,
    sqlite3.SQLITE_LIMIT_EXPR_DEPTH: 100,
    sqlite3.SQLITE_LIMIT_COMPOUND_SELECT: 8,
    sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH: 200,
    sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER: 32,
    sqlite3.SQLITE_LIMIT_FUNCTION_ARG: 8,
    sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH: 0,
}


class DatabaseUnavailable(RuntimeError):
    """Raised when the database file cannot be opened. Carries no path."""


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def open_readonly_connection(path: Path) -> sqlite3.Connection:
    """Open a fresh read-only connection with hardened engine settings.

    The caller owns the connection and must close it. The authorizer is *not*
    installed here; see :func:`app.database.authorizer.install_authorizer`.
    """
    path = Path(path)
    if not path.is_file():
        raise DatabaseUnavailable("database is not available")
    try:
        conn = sqlite3.connect(
            _readonly_uri(path),
            uri=True,
            timeout=1.0,
            isolation_level=None,          # no implicit transactions
            check_same_thread=True,
            cached_statements=0,
        )
    except sqlite3.Error as exc:  # pragma: no cover - depends on filesystem state
        raise DatabaseUnavailable("database is not available") from exc

    try:
        # Belt and braces on top of mode=ro: reject writes even if the URI were changed.
        conn.execute("PRAGMA query_only = ON")
        # Do not run functions/virtual tables from the schema in an untrusted context.
        conn.execute("PRAGMA trusted_schema = OFF")
        for limit, value in _SQLITE_LIMITS.items():
            conn.setlimit(limit, value)
    except Exception:
        conn.close()
        raise
    return conn
