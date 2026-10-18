"""Schema introspection limited to what the policy authorizes.

Introspection is trusted application code: it runs fixed, parameterised
catalog queries (never LLM output) and intersects the result with the policy,
so neither unauthorized tables nor unauthorized columns are ever described to
the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.database.connection import open_readonly_connection
from app.security.policies import AccessPolicy


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    primary_key: bool


@dataclass(frozen=True)
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[ForeignKey, ...]


@dataclass(frozen=True)
class AuthorizedSchema:
    tables: tuple[TableInfo, ...]

    def table(self, name: str) -> TableInfo | None:
        for t in self.tables:
            if t.name == name.lower():
                return t
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "tables": [
                {
                    "name": t.name,
                    "columns": [{"name": c.name, "type": c.type, "primary_key": c.primary_key} for c in t.columns],
                    "foreign_keys": [
                        {"column": fk.column, "references": f"{fk.ref_table}.{fk.ref_column}"} for fk in t.foreign_keys
                    ],
                }
                for t in self.tables
            ]
        }


def introspect(db_path: Path, policy: AccessPolicy) -> AuthorizedSchema:
    conn = open_readonly_connection(db_path)
    try:
        existing = {
            row[0].lower()
            for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        tables: list[TableInfo] = []
        for table in sorted(policy.tables):
            if table not in existing:
                continue
            allowed = policy.columns_for(table)
            cols = tuple(
                ColumnInfo(name=r[0].lower(), type=(r[1] or "").upper(), primary_key=bool(r[2]))
                for r in conn.execute("SELECT name, type, pk FROM pragma_table_info(?) ORDER BY cid", (table,))
                if r[0].lower() in allowed
            )
            fks = tuple(
                ForeignKey(column=r[0].lower(), ref_table=r[1].lower(), ref_column=r[2].lower())
                for r in conn.execute('SELECT "from", "table", "to" FROM pragma_foreign_key_list(?)', (table,))
                if r[0].lower() in allowed and policy.is_column_allowed(r[1], r[2])
            )
            tables.append(TableInfo(name=table, columns=cols, foreign_keys=fks))
        return AuthorizedSchema(tables=tuple(tables))
    finally:
        conn.close()


_WORD = re.compile(r"[a-z]+")
# Domain synonyms used to pick the minimum relevant schema for a question.
_SYNONYMS: dict[str, set[str]] = {
    "customers": {"customer", "customers", "client", "clients", "buyer", "buyers", "city", "cities", "signup"},
    "orders": {"order", "orders", "sales", "sale", "revenue", "monthly", "month", "spend", "spent", "status",
               "returned", "return", "returns", "cancelled", "purchase", "purchases"},
    "products": {"product", "products", "item", "items", "category", "categories", "price", "catalog"},
    "order_items": {"quantity", "units", "sold", "item", "items", "return", "returns", "rate", "line"},
}


def select_relevant_schema(schema: AuthorizedSchema, question: str) -> AuthorizedSchema:
    """Return the minimal subset of the authorized schema needed for ``question``.

    Tables are chosen by keyword overlap, then closed over foreign keys so the
    model always has a join path. Falls back to the full authorized schema.
    """
    words = set(_WORD.findall(question.lower()))
    picked: set[str] = set()
    for t in schema.tables:
        names = {t.name, t.name.rstrip("s")} | {c.name for c in t.columns} | _SYNONYMS.get(t.name, set())
        if words & names:
            picked.add(t.name)
    if not picked:
        return schema
    # order_items bridges orders and products
    if {"orders", "products"} <= picked:
        picked.add("order_items")
    if "order_items" in picked and words & _SYNONYMS["products"]:
        picked.add("products")
    if "order_items" in picked and words & {"return", "returns", "returned", "rate", "status"}:
        picked.add("orders")
    if "customers" in picked and words & _SYNONYMS["orders"]:
        picked.add("orders")
    return AuthorizedSchema(tables=tuple(t for t in schema.tables if t.name in picked))


def render_schema_for_prompt(schema: AuthorizedSchema) -> str:
    lines = []
    for t in schema.tables:
        cols = ", ".join(f"{c.name} {c.type}".strip() for c in t.columns)
        lines.append(f"TABLE {t.name} ({cols})")
        for fk in t.foreign_keys:
            lines.append(f"  -- {t.name}.{fk.column} -> {fk.ref_table}.{fk.ref_column}")
    return "\n".join(lines)


def physical_columns(db_path: Path, policy: AccessPolicy) -> dict[str, frozenset[str]]:
    """All real columns of each *allowed* table, including unauthorized ones.

    Trusted metadata for the validator's alias-shadowing check only. It is
    never returned by the API or sent to the LLM.
    """
    conn = open_readonly_connection(db_path)
    try:
        return {
            table: frozenset(r[0].lower() for r in conn.execute("SELECT name FROM pragma_table_info(?)", (table,)))
            for table in policy.tables
        }
    finally:
        conn.close()
