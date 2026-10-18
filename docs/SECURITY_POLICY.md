# Security Policy: Phase 1

The policy is code (`app/security/policies.py`), immutable at runtime, and never influenced by
LLM output or request data.

## Data access

| Table | Readable columns | Notes |
|---|---|---|
| `customers` | customer_id, name, city, created_at | `email` is PII and **denied** |
| `orders` | order_id, customer_id, order_date, total_amount, status | |
| `products` | product_id, product_name, category, price | |
| `order_items` | order_id, product_id, quantity, unit_price | |
| `admin_users` | none | exists in the DB, **denied** entirely |
| `sqlite_*` | none | internal tables denied |

`COUNT(*)` on an allowed table is permitted (no column values are read).

## SQL rules (AST validator)

1. Exactly one statement; the root must be `SELECT` or `UNION/INTERSECT/EXCEPT` of SELECTs.
2. Rejected node types: INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, ATTACH, DETACH, PRAGMA,
   transactions, SET, SELECT INTO, VALUES, placeholders/parameters, LATERAL, PIVOT, NATURAL JOIN,
   table-valued functions, schema-qualified names, recursive CTEs.
3. Every base table must be allowlisted; CTE names may not shadow real tables.
4. Every column is resolved per scope (joins, aliases, CTEs, derived tables, correlated
   sub-queries). Unknown or ambiguous references are rejected. Output-alias references are
   allowed only when the alias cannot shadow any physical column (SQLite prefers real columns).
5. `SELECT *` and `t.*` are rejected.
6. Function allowlist: aggregates (COUNT, SUM, AVG, MIN, MAX, TOTAL), ROUND, ABS, COALESCE,
   IFNULL, NULLIF, LOWER, UPPER, LENGTH, SUBSTR, TRIM, CAST, CASE, IIF, DATE, DATETIME,
   STRFTIME, JULIANDAY, CURRENT_DATE/TIMESTAMP, ROW_NUMBER, RANK, DENSE_RANK, EXISTS, LIKE.
7. Complexity caps (configurable): ≤ 600 AST nodes, ≤ 6 joins, ≤ 3 set operations,
   ≤ 3 levels of SELECT nesting, ≤ 256-char string literals, ≤ 5,000-char query.
8. Identifiers must match `[A-Za-z_][A-Za-z0-9_]{0,63}` and may not start with `sqlite_`.

## Row limits

- Every `LIMIT`/`OFFSET` anywhere in the tree must be a bare, non-negative base-10 integer
  literal of at most 18 digits. That rules out `LIMIT -1` (unlimited in SQLite), expressions,
  casts, sub-queries, strings, floats, hex, and overflow values.
- `LIMIT ≤ SQLY_MAX_ROWS` (default 100); `OFFSET ≤ SQLY_MAX_OFFSET` (default 10,000).
- A missing root `LIMIT` gets `SQLY_DEFAULT_LIMIT` (default 50). An inner `LIMIT` never counts
  as the outer limit.
- The executor caps rows again with `fetchmany(max_rows + 1)` and caps serialized size
  (`SQLY_MAX_RESPONSE_BYTES`). This holds even if the SQL limit were bypassed.

## Database-level enforcement (authorizer)

- Allowed: `SQLITE_SELECT`; `SQLITE_READ` of allowlisted `table.column` in `main`; the table-level
  READ special case (empty column) for allowlisted tables; `SQLITE_FUNCTION` for allowlisted names.
- Everything else returns `SQLITE_DENY`. `SQLITE_IGNORE` is never used, because it would
  silently turn denied columns into NULL.
- Connections are opened `mode=ro` with `PRAGMA query_only=ON`, `trusted_schema=OFF`,
  `SQLITE_LIMIT_ATTACHED=0`, and further engine limits.

## Execution limits

- Wall-clock deadline `SQLY_QUERY_TIMEOUT_MS` (default 2,000 ms) via the progress handler.
- Request body ≤ `SQLY_MAX_BODY_BYTES` (16 KiB); question ≤ 1,000 chars.

## LLM boundary

- The model sees only the minimal authorized schema, the sanitized question, and template
  feedback (codes plus fixed messages). It never sees raw DB errors, paths, stack traces,
  row values, or hidden schema.
- At most 2 corrections (hard ceiling in code). Timeouts, authorizer denials, and DB
  unavailability end the loop immediately.
- An authorizer denial of a validator-approved query is logged as a `SECURITY` event.

## Error handling

Clients get stable codes (`INVALID_LIMIT`, `QUERY_TIMEOUT`, …) and fixed messages, plus a
server-generated `request_id`. Details go to server logs only, keyed by that id.
