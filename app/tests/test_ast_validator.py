from __future__ import annotations

import pytest
import sqlglot

from app.security.ast_validator import IssueCode, SQLValidator, ValidatorConfig
from app.security.limits import LimitPolicy
from app.security.policies import DEFAULT_POLICY
from app.security.validated import ValidatedQuery
from app.tests.conftest import codes

# ----------------------------------------------------------------- accepted

VALID = {
    "simple_select": "SELECT c.name, c.city FROM customers c WHERE c.city = 'Pune' LIMIT 5",
    "aggregation_join": (
        "SELECT c.customer_id, c.name, ROUND(SUM(o.total_amount), 2) AS revenue FROM customers c "
        "JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.name ORDER BY revenue DESC LIMIT 10"
    ),
    "three_way_join": (
        "SELECT p.product_name, SUM(oi.quantity) AS units FROM products p JOIN order_items oi "
        "ON oi.product_id = p.product_id JOIN orders o ON o.order_id = oi.order_id "
        "WHERE o.status = 'completed' GROUP BY p.product_name ORDER BY units DESC LIMIT 5"
    ),
    "cte": (
        "WITH rev AS (SELECT o.customer_id, SUM(o.total_amount) AS total FROM orders o GROUP BY o.customer_id) "
        "SELECT c.name, rev.total FROM rev JOIN customers c ON c.customer_id = rev.customer_id ORDER BY rev.total DESC LIMIT 5"
    ),
    "in_subquery": (
        "SELECT p.product_name FROM products p WHERE p.product_id IN "
        "(SELECT oi.product_id FROM order_items oi WHERE oi.quantity >= 3) LIMIT 20"
    ),
    "correlated_exists": (
        "SELECT c.name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o "
        "WHERE o.customer_id = c.customer_id AND o.status = 'returned') LIMIT 20"
    ),
    "derived_table": "SELECT s.city, s.n FROM (SELECT c.city, COUNT(*) AS n FROM customers c GROUP BY c.city) s LIMIT 10",
    "count_star": "SELECT COUNT(*) AS n FROM orders",
    "strftime_alias_group": (
        "SELECT strftime('%Y-%m', o.order_date) AS month, SUM(o.total_amount) AS sales FROM orders o "
        "GROUP BY month ORDER BY month LIMIT 12"
    ),
    "case_expression": (
        "SELECT o.status, SUM(CASE WHEN o.total_amount > 500 THEN 1 ELSE 0 END) AS big FROM orders o GROUP BY o.status"
    ),
    "union_authorized": "SELECT c.city FROM customers c UNION SELECT p.category FROM products p LIMIT 10",
    "like": "SELECT c.name FROM customers c WHERE c.name LIKE 'A%' LIMIT 5",
}


@pytest.mark.parametrize("sql", VALID.values(), ids=VALID.keys())
def test_valid_queries_are_accepted(validator: SQLValidator, sql: str) -> None:
    result = validator.validate(sql)
    assert result.ok, result.issues
    assert isinstance(result.query, ValidatedQuery)
    # Executable SQL is the serialization of the validated AST and itself re-validates.
    assert validator.validate(result.query.sql).ok


def test_executable_sql_is_reserialized_not_original(validator: SQLValidator) -> None:
    raw = "select c.name from customers c /* sneaky comment */ where c.city='Pune'"
    result = validator.validate(raw)
    assert result.ok
    assert result.query.sql != raw
    assert "sneaky" not in result.query.sql
    assert result.query.sql == sqlglot.parse_one(result.query.sql, read="sqlite").sql(dialect="sqlite")


# ----------------------------------------------------------------- rejected

REJECTED = {
    # unauthorized tables
    "unauthorized_table": ("SELECT a.username FROM admin_users a", IssueCode.TABLE),
    "sqlite_master": ("SELECT name FROM sqlite_master", IssueCode.IDENTIFIER),
    "schema_qualified": ("SELECT c.name FROM main.customers c", IssueCode.TABLE),
    "table_valued_function": ("SELECT name FROM pragma_table_info('customers')", IssueCode.FORBIDDEN),
    "cte_shadows_table": ("WITH customers AS (SELECT 1 AS name) SELECT name FROM customers", IssueCode.IDENTIFIER),
    "unauthorized_table_in_subquery": (
        "SELECT c.name FROM customers c WHERE c.customer_id IN (SELECT a.user_id FROM admin_users a)", IssueCode.TABLE),
    "unauthorized_table_in_cte": (
        "WITH x AS (SELECT a.password_hash AS h FROM admin_users a) SELECT x.h FROM x", IssueCode.TABLE),
    # unauthorized columns
    "unauthorized_column_qualified": ("SELECT c.email FROM customers c", IssueCode.COLUMN),
    "unauthorized_column_unqualified": ("SELECT email FROM customers", IssueCode.COLUMN),
    "unauthorized_column_in_where": ("SELECT c.name FROM customers c WHERE c.email LIKE '%a%'", IssueCode.COLUMN),
    "unauthorized_column_in_order": ("SELECT c.name FROM customers c ORDER BY c.email", IssueCode.COLUMN),
    "alias_shadows_hidden_column": ("SELECT c.name AS email FROM customers c WHERE email = 'x'", IssueCode.COLUMN),
    "column_through_derived_table": (
        "SELECT s.email FROM (SELECT c.name FROM customers c) s", IssueCode.COLUMN),
    "unknown_alias": ("SELECT x.name FROM customers c", IssueCode.UNKNOWN_REFERENCE),
    "ambiguous_column": (
        "SELECT customer_id FROM customers c JOIN orders o ON o.customer_id = c.customer_id", IssueCode.AMBIGUOUS),
    # stars
    "select_star": ("SELECT * FROM customers", IssueCode.STAR),
    "qualified_star": ("SELECT c.* FROM customers c", IssueCode.STAR),
    "star_in_subquery": ("SELECT s.name FROM (SELECT * FROM customers) s", IssueCode.STAR),
    # UNION exfiltration
    "union_unauthorized_table": (
        "SELECT c.name FROM customers c UNION SELECT a.password_hash FROM admin_users a", IssueCode.TABLE),
    "union_unauthorized_column": (
        "SELECT c.name FROM customers c UNION ALL SELECT c2.email FROM customers c2", IssueCode.COLUMN),
    # statements
    "multiple_statements": ("SELECT c.name FROM customers c; DROP TABLE customers", IssueCode.MULTIPLE_STATEMENTS),
    "stacked_selects": ("SELECT 1; SELECT 2", IssueCode.MULTIPLE_STATEMENTS),
    "insert": ("INSERT INTO customers (name) VALUES ('x')", IssueCode.NOT_SELECT),
    "update": ("UPDATE orders SET status = 'completed'", IssueCode.NOT_SELECT),
    "delete": ("DELETE FROM orders", IssueCode.NOT_SELECT),
    "drop": ("DROP TABLE orders", IssueCode.NOT_SELECT),
    "alter": ("ALTER TABLE orders ADD COLUMN x TEXT", IssueCode.NOT_SELECT),
    "create": ("CREATE TABLE t (x INT)", IssueCode.NOT_SELECT),
    "pragma": ("PRAGMA table_info(customers)", IssueCode.NOT_SELECT),
    "select_into": ("SELECT c.name INTO t FROM customers c", IssueCode.FORBIDDEN),
    "placeholder": ("SELECT c.name FROM customers c WHERE c.customer_id = ?", IssueCode.FORBIDDEN),
    "natural_join": ("SELECT c.name FROM customers c NATURAL JOIN orders o", IssueCode.FORBIDDEN),
    # functions
    "load_extension": ("SELECT load_extension('evil.so')", IssueCode.FUNCTION),
    "randomblob_dos": ("SELECT randomblob(1000000000) AS b", IssueCode.FUNCTION),
    "sqlite_version": ("SELECT sqlite_version() AS v", IssueCode.FUNCTION),
    "hex_function": ("SELECT hex(c.name) AS h FROM customers c", IssueCode.FUNCTION),
    "unapproved_in_where": ("SELECT c.name FROM customers c WHERE instr(c.name, 'a') > 0", IssueCode.FUNCTION),
    # complexity
    "recursive_cte": (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT r.n FROM r", IssueCode.RECURSIVE),
    "deep_nesting": (
        "SELECT c.name FROM customers c WHERE c.customer_id IN (SELECT o.customer_id FROM orders o WHERE o.order_id IN "
        "(SELECT oi.order_id FROM order_items oi WHERE oi.product_id IN (SELECT p.product_id FROM products p WHERE "
        "p.product_id IN (SELECT p2.product_id FROM products p2))))", IssueCode.NESTING),
    # malformed
    "malformed": ("SELEC name FRM customers", IssueCode.MALFORMED),
    "unbalanced": ("SELECT c.name FROM customers c WHERE (c.city = 'x'", IssueCode.MALFORMED),
    "empty": ("   ", IssueCode.EMPTY),
    "nul_byte": ("SELECT c.name FROM customers c\x00", IssueCode.MALFORMED),
}


@pytest.mark.parametrize(("sql", "expected"), REJECTED.values(), ids=REJECTED.keys())
def test_adversarial_queries_are_rejected(validator: SQLValidator, sql: str, expected: str) -> None:
    result = validator.validate(sql)
    assert not result.ok
    assert result.query is None
    assert expected in codes(result), result.issues


@pytest.mark.parametrize("sql", ["ATTACH DATABASE '/tmp/x.db' AS x", "DETACH DATABASE x", "VACUUM", "BEGIN", "COMMIT"])
def test_attach_and_control_statements_rejected(validator: SQLValidator, sql: str) -> None:
    result = validator.validate(sql)
    assert not result.ok


def test_deeply_nested_parentheses_do_not_crash(validator: SQLValidator) -> None:
    sql = "SELECT " + "(" * 400 + "1" + ")" * 400 + " AS x"
    result = validator.validate(sql)
    assert not result.ok  # rejected (complexity/recursion), never an exception


def test_too_many_joins(db_path) -> None:
    v = SQLValidator(DEFAULT_POLICY, LimitPolicy(100, 50, 10), ValidatorConfig(max_joins=2))
    sql = ("SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
           "JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id")
    assert IssueCode.COMPLEXITY in codes(v.validate(sql))


def test_too_long_query(validator: SQLValidator) -> None:
    sql = "SELECT c.name FROM customers c WHERE c.name = '" + "a" * 10_000 + "'"
    assert IssueCode.TOO_LONG in codes(validator.validate(sql))


def test_validated_query_cannot_be_forged() -> None:
    with pytest.raises(PermissionError):
        ValidatedQuery(sql="DELETE FROM orders", effective_limit=1, tables=frozenset(), applied_default_limit=False)


def test_policy_is_immutable() -> None:
    with pytest.raises(TypeError):
        DEFAULT_POLICY.tables["admin_users"] = frozenset({"password_hash"})  # type: ignore[index]
    with pytest.raises(AttributeError):
        DEFAULT_POLICY.allow_star = True  # type: ignore[misc]


def test_issue_messages_do_not_leak_existence(validator: SQLValidator) -> None:
    real = validator.validate("SELECT a.username FROM admin_users a").issues[0].message
    fake = validator.validate("SELECT a.username FROM no_such_table a").issues[0].message
    assert real.replace("admin_users", "X") == fake.replace("no_such_table", "X")
