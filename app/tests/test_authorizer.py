"""The authorizer must stand on its own: these tests run raw SQL with NO AST validation."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.database.authorizer import build_authorizer, install_authorizer
from app.database.connection import open_readonly_connection
from app.security.policies import DEFAULT_POLICY


def run_raw(db_path: Path, sql: str):
    conn = open_readonly_connection(db_path)
    audit = install_authorizer(conn, DEFAULT_POLICY)
    try:
        return conn.execute(sql).fetchall(), audit
    finally:
        conn.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c.name, c.city FROM customers c LIMIT 3",
        "SELECT COUNT(*) FROM orders",  # table-level READ with empty column name
        "SELECT o.order_id FROM orders o WHERE o.customer_id IN (SELECT c.customer_id FROM customers c) LIMIT 1",
        "SELECT ROUND(SUM(total_amount), 2), strftime('%Y', MAX(order_date)) FROM orders",
        "SELECT c.name FROM customers c WHERE c.name LIKE 'A%' LIMIT 1",
    ],
)
def test_authorized_reads_allowed(db_path: Path, sql: str) -> None:
    rows, audit = run_raw(db_path, sql)
    assert rows and audit.denials == []


@pytest.mark.parametrize(
    ("sql", "denied"),
    [
        ("SELECT email FROM customers", ("READ", "customers.email")),
        ("SELECT username FROM admin_users", ("READ", "admin_users.username")),
        ("SELECT COUNT(*) FROM admin_users", ("READ", "admin_users.<table>")),
        ("SELECT c.name AS email FROM customers c WHERE email = 'x'", ("READ", "customers.email")),
        ("SELECT name FROM customers UNION SELECT password_hash FROM admin_users", ("READ", "admin_users.password_hash")),
        ("SELECT name FROM sqlite_schema", ("READ", "sqlite_master.name")),
        ("SELECT hex(name) FROM customers", ("FUNCTION", "hex")),
        ("SELECT load_extension('x')", ("FUNCTION", "load_extension")),
    ],
)
def test_unauthorized_access_denied_at_db_level(db_path: Path, sql: str, denied: tuple[str, str]) -> None:
    with pytest.raises(sqlite3.DatabaseError):
        run_raw(db_path, sql)
    # rerun to inspect the audit trail
    conn = open_readonly_connection(db_path)
    audit = install_authorizer(conn, DEFAULT_POLICY)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(sql)
    conn.close()
    assert denied in audit.denials


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA table_info(customers)",
        "ATTACH DATABASE ':memory:' AS x",
        "DELETE FROM orders",
        "INSERT INTO products (product_name, category, price) VALUES ('x','y',1)",
        "UPDATE orders SET status = 'completed'",
        "DROP TABLE orders",
        "CREATE TEMP TABLE t (x INT)",
        "BEGIN",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r LIMIT 3) SELECT n FROM r",
    ],
)
def test_writes_ddl_pragma_attach_denied(db_path: Path, sql: str) -> None:
    with pytest.raises(sqlite3.DatabaseError):
        run_raw(db_path, sql)


def test_authorizer_never_returns_ignore() -> None:
    cb = build_authorizer(DEFAULT_POLICY)
    for action in range(0, 40):
        for args in [("customers", "email"), ("admin_users", "x"), (None, None), ("orders", "status")]:
            assert cb(action, args[0], args[1], "main", None) in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY)


def test_authorizer_denies_other_databases() -> None:
    cb = build_authorizer(DEFAULT_POLICY)
    assert cb(sqlite3.SQLITE_READ, "customers", "name", "main", None) == sqlite3.SQLITE_OK
    assert cb(sqlite3.SQLITE_READ, "customers", "name", "temp", None) == sqlite3.SQLITE_DENY
    assert cb(sqlite3.SQLITE_READ, "customers", "name", "attached", None) == sqlite3.SQLITE_DENY


def test_authorizer_fails_closed_on_garbage_input() -> None:
    cb = build_authorizer(DEFAULT_POLICY)
    assert cb(sqlite3.SQLITE_READ, None, None, None, None) == sqlite3.SQLITE_DENY
    assert cb(sqlite3.SQLITE_FUNCTION, None, None, None, None) == sqlite3.SQLITE_DENY
    assert cb(9999, "customers", "name", "main", None) == sqlite3.SQLITE_DENY


def test_connections_do_not_share_authorizer_state(db_path: Path) -> None:
    """A connection without an authorizer elsewhere must not affect a protected one, and vice versa."""
    errors: list[BaseException] = []

    def protected() -> None:
        try:
            for _ in range(20):
                with pytest.raises(sqlite3.DatabaseError):
                    run_raw(db_path, "SELECT email FROM customers")
        except BaseException as exc:  # noqa: BLE001  # pragma: no cover
            errors.append(exc)

    def unprotected() -> None:
        for _ in range(20):
            conn = open_readonly_connection(db_path)  # no authorizer: trusted internal use
            conn.execute("SELECT email FROM customers LIMIT 1").fetchall()
            conn.close()

    threads = [threading.Thread(target=protected) for _ in range(4)] + [threading.Thread(target=unprotected) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_readonly_connection_rejects_writes_without_authorizer(db_path: Path) -> None:
    conn = open_readonly_connection(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM orders")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("ATTACH DATABASE ':memory:' AS x")
    finally:
        conn.close()
