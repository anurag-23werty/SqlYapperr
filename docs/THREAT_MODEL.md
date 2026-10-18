# Threat Model

## Assets
1. Users' uploaded data, especially hidden or sensitive columns.
2. Other users' datasets and accounts.
3. Credentials: passwords, session tokens, the Groq API key, `SQLY_SECRET_KEY`.
4. Service availability and the Groq free-tier quota.

## Trust boundaries

```
[Browser] ─TB1─► [API + auth] ─► [Agent] ─TB2─► [Groq LLM] ─TB3─► [AST guard] ─TB4─► [SQLite + authorizer]
                                     ▲
                    [Uploaded CSV] ──┘  (data values are untrusted text, TB5)
```

| Boundary | Untrusted input | Main controls |
|---|---|---|
| TB1 browser → API | requests, cookies, uploads | sessions, CSRF + Origin, body caps, strict schemas, rate limits |
| TB2 agent → LLM | the question (may contain an injection) | input guard, fencing, router, no tools |
| TB3 LLM → guard | **model output is adversarial** | AST allowlist, rewrite, re-validation |
| TB4 guard → DB | guard bugs | authorizer, read-only connection, engine limits, deadline |
| TB5 data → prompt | cell values with embedded instructions | samples/rows fenced and truncated, sensitive values never sent |

## Threats and mitigations

| # | Threat | Mitigations |
|---|---|---|
| T1 | Direct prompt injection ("ignore your rules…") | input guard blocks it; router `unsafe`; even if bypassed, SQL is checked by the guard and authorizer |
| T2 | Indirect injection through CSV values | values fenced, delimiter tags stripped, truncated to 30–40 chars; summaries are text only |
| T3 | Model emits destructive SQL | single SELECT only; read-only connection; authorizer denies writes |
| T4 | Exfiltrating hidden columns (UNION, aliases, sub-queries) | scope-aware column checks; alias-shadowing check; authorizer READ allowlist |
| T5 | Reading another user's data | ownership in every lookup; one SQLite file per dataset; per-dataset policy |
| T6 | Resource exhaustion (cartesian joins, huge LIMIT, deep nesting) | join-ON rule, LIMIT literal rules, complexity caps, deadline, row/byte caps |
| T7 | CSRF from another site | SameSite cookie, CSRF token header, Origin check |
| T8 | XSS through data or model output | strict CSP, no inline script, `textContent` rendering only |
| T9 | Credential stuffing / brute force | scrypt, per-IP and per-email limits, uniform errors |
| T10 | Stolen app database | passwords scrypt-hashed; session tokens HMAC-hashed with a secret outside the DB |
| T11 | Malicious CSV (identifier injection, binary, huge files) | identifier sanitizer + regex re-check, size/row/column limits, NUL detection |
| T12 | Path traversal through ids | server-generated hex ids, validated before building paths |
| T13 | Groq quota drain | chat rate limit per user, 8B model for routing/summaries, bounded retries |
| T14 | Secret leakage in errors or logs | fixed error codes; the API key is never logged or returned |

## Out of scope (see LIMITATIONS)
Multi-process rate limiting, MFA, password reset by email, row-level permissions within a dataset,
audit-log shipping, malware scanning of uploads.
