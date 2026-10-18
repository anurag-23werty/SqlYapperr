from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.execution.query_executor import ExecutionError, ExecutionErrorCode, ExecutionLimits, QueryExecutor
from app.security.ast_validator import SQLValidator
from app.security.policies import DEFAULT_POLICY

SLOW = "SELECT COUNT(*) AS n FROM order_items a CROSS JOIN order_items b CROSS JOIN order_items c"


def test_structured_result(validator: SQLValidator, executor: QueryExecutor) -> None:
    q = validator.validate("SELECT c.customer_id, c.name FROM customers c ORDER BY c.customer_id LIMIT 3").query
    result = executor.execute(q, request_id="abc123")
    assert result.request_id == "abc123"
    assert result.columns == ["customer_id", "name"]
    assert result.row_count == 3 and len(result.rows) == 3
    assert [r[0] for r in result.rows] == [1, 2, 3]
    assert result.duration_ms >= 0 and not result.truncated


def test_executor_refuses_raw_strings(executor: QueryExecutor) -> None:
    with pytest.raises(TypeError):
        executor.execute("SELECT 1")  # type: ignore[arg-type]


def test_query_timeout(validator: SQLValidator, db_path: Path) -> None:
    query = validator.validate(SLOW)
    assert query.ok, "slow query must pass validation so the *timeout* is what stops it"
    fast_deadline = QueryExecutor(db_path, DEFAULT_POLICY, ExecutionLimits(timeout_ms=50, progress_ops=100))
    with pytest.raises(ExecutionError) as info:
        fast_deadline.execute(query.query)
    assert info.value.code == ExecutionErrorCode.TIMEOUT


def test_timeout_is_independent_per_request(validator: SQLValidator, db_path: Path) -> None:
    fast_deadline = QueryExecutor(db_path, DEFAULT_POLICY, ExecutionLimits(timeout_ms=50, progress_ops=100))
    with pytest.raises(ExecutionError):
        fast_deadline.execute(validator.validate(SLOW).query)
    # the next query on the same executor is unaffected
    ok = fast_deadline.execute(validator.validate("SELECT c.name FROM customers c LIMIT 1").query)
    assert ok.row_count == 1


def test_error_messages_do_not_leak(validator: SQLValidator, db_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Runtime errors become generic codes: no paths, SQL internals or DB messages."""
    q = validator.validate("SELECT c.name FROM customers c LIMIT 1").query
    missing = QueryExecutor(db_path.parent / "does-not-exist.db", DEFAULT_POLICY, ExecutionLimits())
    with pytest.raises(ExecutionError) as info:
        missing.execute(q)
    err = info.value
    assert err.code == ExecutionErrorCode.DATABASE_UNAVAILABLE
    text = f"{err} {err.public_message}"
    for secret in (str(db_path.parent), "does-not-exist", ".db", "Traceback", "sqlite3"):
        assert secret not in text

    fast_deadline = QueryExecutor(db_path, DEFAULT_POLICY, ExecutionLimits(timeout_ms=50, progress_ops=100))
    with caplog.at_level(logging.WARNING), pytest.raises(ExecutionError) as info:
        fast_deadline.execute(validator.validate(SLOW).query)
    assert "interrupted" not in info.value.public_message


def test_blob_values_are_json_safe(executor: QueryExecutor) -> None:
    from app.execution.query_executor import _json_safe

    assert _json_safe(b"\x00\x01") == {"$base64": "AAE="}
    assert _json_safe(1.5) == 1.5 and _json_safe(None) is None
