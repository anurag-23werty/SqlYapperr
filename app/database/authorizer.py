"""SQLite authorizer: the database-level enforcement layer.

This layer is independent of the AST validator. Even if the validator had a
bug, SQLite itself refuses to compile a statement that reads a column, calls a
function or performs an operation not on this allowlist.

SQLite semantics worth knowing:

* The callback runs at *prepare* time, once per access, with
  ``(action, arg1, arg2, db_name, trigger_or_view)``.
* ``SQLITE_READ`` arrives once per referenced column, with ``arg1`` = table and
  ``arg2`` = column. Returning ``SQLITE_IGNORE`` would silently substitute NULL,
  so we never return it: a denied read fails the whole statement.
* ``SQLITE_SELECT`` is the statement itself; ``SQLITE_FUNCTION`` carries the
  function name in ``arg2``. ``LIKE`` is a function in SQLite.
* If the callback raises, Python's sqlite3 module treats it as a denial. We
  catch everything anyway and return ``SQLITE_DENY`` (fail closed).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from app.security.policies import AccessPolicy

log = logging.getLogger(__name__)

AuthorizerCallback = Callable[[int, str | None, str | None, str | None, str | None], int]


@dataclass
class AuthorizerAudit:
    """Per-connection record of denials (never shared between requests)."""

    denials: list[tuple[str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def deny(self, action: str, detail: str) -> int:
        with self._lock:
            if len(self.denials) < 50:
                self.denials.append((action, detail))
        return sqlite3.SQLITE_DENY


_ACTION_NAMES = {
    getattr(sqlite3, n): n
    for n in (
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_DELETE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW", "SQLITE_INSERT", "SQLITE_PRAGMA", "SQLITE_READ", "SQLITE_SELECT",
        "SQLITE_TRANSACTION", "SQLITE_UPDATE", "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_ALTER_TABLE",
        "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE",
        "SQLITE_FUNCTION", "SQLITE_SAVEPOINT", "SQLITE_RECURSIVE",
    )
    if hasattr(sqlite3, n)
}


def build_authorizer(policy: AccessPolicy, audit: AuthorizerAudit | None = None) -> AuthorizerCallback:
    audit = audit or AuthorizerAudit()
    allowed_db = policy.database_name

    def authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None) -> int:
        try:
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK

            if action == sqlite3.SQLITE_READ:
                table, column = (arg1 or ""), (arg2 or "")
                # Special case: SQLite reports a table that is in FROM but whose
                # columns are not read (e.g. COUNT(*)) as a READ with an empty
                # column name and no database name. That is table-level access
                # with no column values, so it is allowed for allowlisted tables only.
                if column == "" and db_name is None:
                    if policy.is_table_allowed(table):
                        return sqlite3.SQLITE_OK
                    return audit.deny("READ", f"{table}.<table>")
                if db_name != allowed_db:
                    return audit.deny("READ", f"db:{db_name}")
                if policy.is_column_allowed(table, column):
                    return sqlite3.SQLITE_OK
                return audit.deny("READ", f"{table}.{column}")

            if action == sqlite3.SQLITE_FUNCTION:
                name = (arg2 or "").lower()
                if name in policy.sqlite_functions:
                    return sqlite3.SQLITE_OK
                return audit.deny("FUNCTION", name)

            # Everything else: writes, DDL, PRAGMA, ATTACH/DETACH, transactions,
            # recursive CTEs, virtual tables, savepoints, ANALYZE, REINDEX ... denied.
            return audit.deny(_ACTION_NAMES.get(action, f"ACTION_{action}"), arg1 or "")
        except Exception:  # pragma: no cover - defensive: never fail open
            return sqlite3.SQLITE_DENY

    return authorizer


def install_authorizer(conn: sqlite3.Connection, policy: AccessPolicy) -> AuthorizerAudit:
    """Install a fresh authorizer on ``conn`` and return its private audit log.

    Must be the last configuration step before running untrusted SQL: once it
    is installed, PRAGMA and other setup statements are denied as well.
    """
    audit = AuthorizerAudit()
    conn.set_authorizer(build_authorizer(policy, audit))
    return audit
