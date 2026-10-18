"""Access-control policy: the single source of truth for what may be read.

The policy is defined in code, is immutable, and is never derived from LLM
output. Both the AST validator and the SQLite authorizer consume this same
object, but they enforce it independently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


def _freeze(tables: Mapping[str, frozenset[str] | set[str]]) -> Mapping[str, frozenset[str]]:
    return MappingProxyType({t.lower(): frozenset(c.lower() for c in cols) for t, cols in tables.items()})


@dataclass(frozen=True)
class AccessPolicy:
    """Allowlist of tables/columns and SQL functions.

    ``tables`` maps a lower-case table name to the lower-case columns that may be
    read. Anything not listed is denied, including tables that exist in the
    database (for example ``admin_users``) and columns that exist on an allowed
    table (for example ``customers.email``, which is PII).
    """

    tables: Mapping[str, frozenset[str]]
    # Function names as SQLGlot reports them (``Func.sql_name()`` or the raw
    # name of an anonymous function), upper-case.
    ast_functions: frozenset[str]
    # Function names exactly as SQLite reports them to the authorizer, lower-case.
    sqlite_functions: frozenset[str]
    allow_star: bool = False
    allow_set_operations: bool = True
    database_name: str = field(default="main")

    def is_table_allowed(self, table: str) -> bool:
        return table.lower() in self.tables

    def is_column_allowed(self, table: str, column: str) -> bool:
        cols = self.tables.get(table.lower())
        return cols is not None and column.lower() in cols

    def columns_for(self, table: str) -> frozenset[str]:
        return self.tables.get(table.lower(), frozenset())


# SQLGlot canonical names. Several SQLite functions map to dialect-neutral nodes
# (e.g. strftime -> TIME_TO_STR, iif -> IF), so both spellings are listed.
_AST_FUNCTIONS = frozenset(
    {
        # aggregates
        "COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL",
        # scalar
        "ROUND", "ABS", "COALESCE", "IFNULL", "NULLIF", "LOWER", "UPPER", "LENGTH",
        "SUBSTRING", "SUBSTR", "TRIM", "CAST", "CASE", "IF", "IIF",
        # dates
        "DATE", "STRFTIME", "TIME_TO_STR", "TS_OR_DS_TO_DATE", "DATE_STR_TO_DATE",
        "STR_TO_TIME", "TIME_STR_TO_TIME", "JULIANDAY", "CURRENT_DATE", "DATETIME", "TS_OR_DS_TO_TIMESTAMP", "CURRENT_TIMESTAMP",
        # predicates implemented as function nodes
        "EXISTS",
        # window
        "ROW_NUMBER", "RANK", "DENSE_RANK",
    }
)

# Names SQLite passes to SQLITE_FUNCTION. LIKE is implemented as a function in SQLite.
_SQLITE_FUNCTIONS = frozenset(
    {
        "count", "sum", "avg", "min", "max", "total",
        "round", "abs", "coalesce", "ifnull", "nullif", "lower", "upper", "length",
        "substr", "substring", "trim", "iif", "like",
        "date", "datetime", "strftime", "julianday", "current_date", "current_timestamp",
        "row_number", "rank", "dense_rank",
    }
)

DEFAULT_POLICY = AccessPolicy(
    tables=_freeze(
        {
            # email is deliberately excluded (PII)
            "customers": {"customer_id", "name", "city", "created_at"},
            "orders": {"order_id", "customer_id", "order_date", "total_amount", "status"},
            "products": {"product_id", "product_name", "category", "price"},
            "order_items": {"order_id", "product_id", "quantity", "unit_price"},
            # admin_users exists in the database but is NOT authorized
        }
    ),
    ast_functions=_AST_FUNCTIONS,
    sqlite_functions=_SQLITE_FUNCTIONS,
)
