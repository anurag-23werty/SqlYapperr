# Security Policy

## 1. Who can see what

- Every dataset belongs to one user. Lookups always filter by `id AND user_id`, so another user's
  dataset id behaves exactly like a non-existent one (404).
- The access policy for a dataset is built from its metadata: **only non-hidden tables and columns**
  are readable. It is immutable for the duration of a request and never derived from model output.
- Columns whose names look sensitive (`email`, `phone`, `mobile`, `password`, `token`, `api_key`,
  `ssn`, `aadhaar`, `pan_no`, `passport`, `card_number`, `cvv`, `iban`, `account_number`, `dob`,
  `address`, `zip`, `pincode`, `ip_address`, ...) are hidden on upload, and their statistics and
  samples are never stored for the prompt. Owners can hide or show any column or table.
- Ingestion metadata (`_sqly_columns`) and SQLite internals (`sqlite_*`) are never readable.

## 2. SQL rules (AST guard)

1. Exactly one statement: `SELECT` or a set operation of SELECTs.
2. Rejected: INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, ATTACH, DETACH, PRAGMA, transactions, SET,
   SELECT INTO, VALUES, placeholders, LATERAL, PIVOT, NATURAL JOIN, table-valued functions,
   schema-qualified names, recursive CTEs.
3. **No `*` anywhere.** `SELECT *` and `t.*` are rejected. `COUNT(*)` is rewritten to `COUNT(1)`.
4. **Every JOIN needs an ON/USING condition.** Comma joins and CROSS JOINs are rejected.
5. Tables and columns must be in the dataset policy. Resolution is scope-aware; unknown or
   ambiguous references are rejected. Output aliases can't shadow real columns.
6. Functions are allowlisted (aggregates, ROUND, ABS, COALESCE, IFNULL, NULLIF, LOWER, UPPER,
   LENGTH, SUBSTR, TRIM, CAST, CASE, IIF, DATE, DATETIME, STRFTIME, JULIANDAY, window ranks, EXISTS, LIKE).
7. Complexity caps: ≤ 600 AST nodes, ≤ 6 joins, ≤ 3 set operations, ≤ 3 nested SELECT levels,
   string literals ≤ 256 chars, query ≤ 5,000 chars.
8. LIMIT/OFFSET anywhere must be a bare non-negative integer literal. `LIMIT ≤ 100`, `OFFSET ≤ 10,000`.
   A missing root LIMIT gets 50.
9. The executed SQL is the serialization of the validated tree, re-parsed and re-validated.

## 3. Database layer

- Connections: `mode=ro`, `PRAGMA query_only=ON`, `trusted_schema=OFF`, `SQLITE_LIMIT_ATTACHED=0`,
  plus engine limits on SQL length, expression depth, compound selects and string size.
- The SQLite authorizer allows only SELECT, column READs of the dataset policy, and allowlisted
  functions. Everything else is denied (never `IGNORE`). A denial of a guard-approved query is
  logged as a security event and stops the agent.
- Deadline: `SQLY_QUERY_TIMEOUT_MS` (3 s) through the progress handler. Rows are capped at 100 and
  the serialized response at 1 MB.

## 4. LLM and prompt injection

- **Input guard** (before any model call): Unicode NFKC, strips control/zero-width/bidi characters,
  blocks instruction-override attempts, prompt-extraction attempts, role/delimiter tags, and
  write intent (`drop table`, `delete from`, ...).
- **Spotlighting**: the question, history, schema samples, feedback and result rows go inside
  tags, are stripped of delimiter-like tags, truncated, and the system prompts say tagged content
  is data only.
- **Router** labels each request as data question, out-of-scope, chitchat or unsafe. Only data
  questions reach SQL generation.
- **Least privilege**: the model has no tools, no database handle, and no way to change policy
  or limits. Its output is parsed as JSON. Anything else counts as a failed attempt.
- At most **2 corrections** (hard ceiling in code). Rate limits, auth errors, timeouts and
  authorizer denials stop the loop immediately.
- Summaries are plain text built from at most 20 fenced rows, and the UI renders them with `textContent`.

## 5. Web and auth

- Passwords: scrypt (n=2^14, r=8, p=1) with per-user salt. Minimum 10 characters from 2+ character
  classes. Unknown emails get a dummy hash check, so response timing doesn't reveal accounts.
- Sessions: 256-bit random token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` in production).
  The server stores only `HMAC-SHA256(SQLY_SECRET_KEY, token)`. Sessions expire after 7 days by default.
- CSRF: every state-changing request needs the session's CSRF token in `X-CSRF-Token`, and a
  matching `Origin` when the browser sends one.
- Rate limits: sign-in by IP and by email, sign-up by IP, chat by user (in-process).
- Headers: strict CSP (`script-src 'self'`, no inline script), `X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy: no-referrer`, `no-store` on API responses.
- Request bodies are capped at 16 KB (uploads at `SQLY_MAX_UPLOAD_MB`). Validation errors never echo input.
- `SQLY_ENV=production` refuses to start without a strong secret and secure cookies, and disables `/docs`.

## 6. Operations security

- `/metrics` needs a bearer token (`SQLY_METRICS_TOKEN`, constant-time compare). It is disabled in
  production without one, and `/metrics` and `/ready` are never routed by the public proxy.
- The container runs as an unprivileged user (uid 10001) with a read-only root filesystem, no Linux
  capabilities and `no-new-privileges`. Only Caddy publishes ports.
- Rate-limit keys are SHA-256 hashed before storage, so emails and IPs never sit in clear text.
- Logs and traces carry ids, codes, timings, model names and token counts. They never carry
  prompts, result rows, hidden schema or secrets.
- CI runs pip-audit, gitleaks and a Trivy image scan, and Dependabot keeps dependencies, Actions and
  base images up to date.
