"""Regression tests for the LIMIT/OFFSET bypass class (SQLite treats LIMIT -1 as unlimited)."""

from __future__ import annotations

import pytest

from app.execution.query_executor import ExecutionLimits, QueryExecutor
from app.security.ast_validator import SQLValidator
from app.security.policies import DEFAULT_POLICY
from app.tests.conftest import codes

BASE = "SELECT o.order_id FROM orders o"

BAD_LIMITS = {
    "negative_one": (f"{BASE} LIMIT -1", "INVALID_LIMIT"),
    "negative_large": (f"{BASE} LIMIT -9999", "INVALID_LIMIT"),
    "negative_parenthesized": (f"{BASE} LIMIT (-1)", "INVALID_LIMIT"),
    "double_negative": (f"{BASE} LIMIT - -1", "INVALID_LIMIT"),
    "expression": (f"{BASE} LIMIT 1 + 1", "INVALID_LIMIT"),
    "expression_to_negative": (f"{BASE} LIMIT 0 - 1", "INVALID_LIMIT"),
    "multiplication": (f"{BASE} LIMIT 10 * 1000", "INVALID_LIMIT"),
    "subquery": (f"{BASE} LIMIT (SELECT 1000)", "INVALID_LIMIT"),
    "cast": (f"{BASE} LIMIT CAST('1000' AS INTEGER)", "INVALID_LIMIT"),
    "string": (f"{BASE} LIMIT '10'", "INVALID_LIMIT"),
    "float": (f"{BASE} LIMIT 2.5", "INVALID_LIMIT"),
    "scientific": (f"{BASE} LIMIT 1e3", "INVALID_LIMIT"),
    "hex": (f"{BASE} LIMIT 0x7fffffff", "INVALID_LIMIT"),
    "int64_overflow": (f"{BASE} LIMIT 9223372036854775808", "INVALID_LIMIT"),
    "huge_overflow": (f"{BASE} LIMIT 99999999999999999999999999", "INVALID_LIMIT"),
    "too_large": (f"{BASE} LIMIT 101", "LIMIT_TOO_LARGE"),
    "way_too_large": (f"{BASE} LIMIT 1000000", "LIMIT_TOO_LARGE"),
    "negative_offset": (f"{BASE} LIMIT 10 OFFSET -1", "INVALID_OFFSET"),
    "offset_expression": (f"{BASE} LIMIT 10 OFFSET 1 + 1", "INVALID_OFFSET"),
    "offset_too_large": (f"{BASE} LIMIT 10 OFFSET 99999999", "INVALID_OFFSET"),
    "comma_form_negative_offset": (f"{BASE} LIMIT -1, 10", "INVALID_OFFSET"),
    "comma_form_negative_count": (f"{BASE} LIMIT 0, -1", "INVALID_LIMIT"),
    "comma_form_too_large": (f"{BASE} LIMIT 0, 5000", "LIMIT_TOO_LARGE"),
    "negative_in_subquery": (
        "SELECT s.order_id FROM (SELECT o.order_id FROM orders o LIMIT -1) s LIMIT 10", "INVALID_LIMIT"),
    "negative_in_cte": (
        "WITH x AS (SELECT o.order_id FROM orders o LIMIT -1) SELECT x.order_id FROM x LIMIT 5", "INVALID_LIMIT"),
    "negative_on_union": (
        "SELECT c.city FROM customers c UNION SELECT p.category FROM products p LIMIT -1", "INVALID_LIMIT"),
}


@pytest.mark.parametrize(("sql", "code"), BAD_LIMITS.values(), ids=BAD_LIMITS.keys())
def test_bad_limits_rejected(validator: SQLValidator, sql: str, code: str) -> None:
    result = validator.validate(sql)
    assert not result.ok, f"accepted: {result.safe_sql}"
    assert code in codes(result), result.issues


def test_missing_limit_gets_default(validator: SQLValidator) -> None:
    result = validator.validate(BASE)
    assert result.ok
    assert result.query.applied_default_limit
    assert result.query.effective_limit == 50
    assert result.query.sql.endswith("LIMIT 50")


def test_missing_limit_on_union_gets_default(validator: SQLValidator) -> None:
    result = validator.validate("SELECT c.city FROM customers c UNION SELECT p.category FROM products p")
    assert result.ok and result.query.effective_limit == 50 and result.query.sql.endswith("LIMIT 50")


@pytest.mark.parametrize("n", [0, 1, 50, 100])
def test_limits_within_bounds_accepted(validator: SQLValidator, n: int) -> None:
    result = validator.validate(f"{BASE} LIMIT {n}")
    assert result.ok and result.query.effective_limit == n and not result.query.applied_default_limit


def test_inner_limit_does_not_make_outer_query_unbounded(validator: SQLValidator) -> None:
    # An inner LIMIT does not count as the outer LIMIT: the root still gets a default.
    result = validator.validate("SELECT s.order_id FROM (SELECT o.order_id FROM orders o LIMIT 5) s")
    assert result.ok and result.query.sql.endswith("LIMIT 50")


def test_executor_caps_rows_even_if_limit_is_larger(validator: SQLValidator, db_path) -> None:
    """Defense in depth: the executor caps rows independently of the SQL LIMIT."""
    small = QueryExecutor(db_path, DEFAULT_POLICY, ExecutionLimits(max_rows=7))
    result = small.execute(validator.validate(f"{BASE} LIMIT 100").query)
    assert result.row_count == 7
    assert result.truncated


def test_response_size_cap(validator: SQLValidator, db_path) -> None:
    tiny = QueryExecutor(db_path, DEFAULT_POLICY, ExecutionLimits(max_rows=100, max_response_bytes=1_024))
    result = tiny.execute(validator.validate("SELECT c.name, c.city, c.created_at FROM customers c LIMIT 100").query)
    assert result.truncated and 0 < result.row_count < 60
