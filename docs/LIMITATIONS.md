# Known Limitations and Unresolved Risks

## Not built in Phase 1 (by design)

- **No API authentication or per-user authorization.** Every caller gets the same policy.
  Add auth and per-principal policies before any shared deployment.
- **No rate limiting or concurrency cap.** One client can send many slow (≤ 2 s) queries in
  parallel. Add a rate limiter and a bounded worker pool.
- **No row-level security.** Column/table allowlists only. Any caller can read every allowed row.
- **No persistent audit log.** Rejections and denials go to process logs only.

## Residual risks

1. **Inference through aggregates.** An allowed column can still leak information through
   predicates (`WHERE c.name LIKE 'A%'`). Hidden columns cannot be used this way, since both
   layers deny them, but a boolean oracle over *allowed* data is inherent to the feature.
2. **Timing side channels.** Execution time can hint at data distribution in allowed tables.
3. **Deadline granularity.** The progress handler fires every N VM instructions. A single
   expensive built-in (e.g. sorting a large intermediate set) can overshoot the deadline
   somewhat. The memory and length limits bound the damage.
4. **sqlglot version coupling.** Function names are matched on sqlglot's canonical names
   (e.g. `strftime` → `TIME_TO_STR`). A sqlglot upgrade could rename nodes. That fails closed
   (queries get rejected), but it needs re-testing. sqlglot is pinned `<26`.
5. **Mock generator.** The default model is keyword-based and only covers the demo questions.
   The Anthropic provider exists but has no live integration test (no network in CI).
6. **Output-alias resolution.** Aliases are allowed in GROUP BY/HAVING/ORDER BY only when they
   cannot shadow a physical column. Some legitimate queries get rejected (false positives),
   which is the intended trade-off.
7. **CTE column lists** (`WITH x(a, b) AS …`) are not resolved and get rejected.
8. **Request size cap** is enforced in the app. A reverse proxy should enforce it as well.
9. **Single SQLite file.** No connection pooling. Each query pays the cost of opening a
   connection (≈ sub-millisecond locally).

## Next phase suggestions

AuthN/Z with per-role policies, rate limiting, a structured audit log, a real LLM provider
with evaluation sets, a Postgres backend (read-only role + statement_timeout + RLS), and
fuzzing the validator against the authorizer (differential testing).
