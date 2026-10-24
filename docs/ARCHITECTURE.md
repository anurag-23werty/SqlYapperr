# Architecture

## End-to-end flow

```mermaid
flowchart TD
    B[Browser: sign-in page + chat UI] -->|HttpOnly session cookie + X-CSRF-Token| MW[Middleware<br/>request id, body caps, CSP]
    MW --> AUTH[Auth: session lookup, CSRF, same-origin]
    AUTH --> DS[Dataset service<br/>ownership-checked lookup]
    DS --> CTX[DatasetContext<br/>policy from non-hidden columns]
    CTX --> G

    subgraph G[LangGraph agent]
      IG[guard_input<br/>normalize + injection / write-intent checks] -->|ok| RT[route<br/>Groq openai/gpt-oss-20b]
      RT -->|data_question| GEN[generate_sql<br/>Groq openai/gpt-oss-120b]
      GEN --> GS[guard_sql<br/>parse → check every node → rewrite]
      GS -->|rejected| GEN
      GS -->|ValidatedQuery| EX[execute<br/>read-only + authorizer + deadline]
      EX -->|retryable failure| GEN
      EX --> SUM[summarize<br/>Groq 8B, data fenced]
    end

    IG -->|blocked| OUT[Response]
    RT -->|out_of_scope / chitchat / unsafe| OUT
    SUM --> OUT
    EX -->|timeout / denied| OUT
```

## The SQL never reaches the database directly

1. The model returns JSON with an `sql` **candidate** string.
2. `guard_sql` parses it with SQLGlot into an AST and walks **every node**. It checks the
   statement type, tables, columns (scope-aware across joins, CTEs, sub-queries and aliases),
   functions, stars, joins, nesting, size and LIMIT/OFFSET literals.
3. The validated tree is normalized (`COUNT(*)` → `COUNT(1)`, a default `LIMIT` is added) and
   **serialized to fresh SQL**. That string is parsed and checked again.
4. Only a `ValidatedQuery` (a capability object only the guard can create) is accepted by the
   executor. A raw string is a type error.
5. The executor opens a new read-only connection, installs a **SQLite authorizer** built from
   the same dataset policy, sets a deadline, runs the query, and caps rows and bytes.

## Components

| Layer | Module | Responsibility |
|---|---|---|
| Config | `config.py`, `env.py` | frozen settings from `.env` / env vars; production guards |
| Auth | `auth/` | scrypt passwords, HMAC-hashed session tokens, CSRF, rate limits |
| Storage | `storage/app_db.py` | users, sessions, datasets, query log (fixed, parameterised SQL) |
| Ingestion | `storage/datasets.py` | CSV → SQLite, safe identifiers, types, dates, indexes, stats, sensitive-column hiding |
| Policy | `security/policies.py` | per-dataset allowlist from non-hidden tables/columns |
| Prompt guard | `security/prompt_guard.py` | input normalization, injection/write-intent detection, fencing |
| AST guard | `security/ast_validator.py`, `limits.py` | parse → check → rewrite → re-check |
| Authorizer | `database/authorizer.py` | engine-level READ/FUNCTION allowlist, deny everything else |
| Executor | `execution/query_executor.py` | deadline, row/byte caps, query plan, safe error codes |
| LLM | `agent/llm.py` | Groq JSON-mode client with retries; mock and scripted clients |
| Agent | `agent/graph.py`, `prompts.py` | LangGraph state machine, bounded corrections, summaries |
| API | `api/` | REST endpoints, dependencies, schemas |
| Web | `web/static/` | vanilla JS SPA, textContent-only rendering, strict CSP |

## Groq model choice (free tier)

| Purpose | Default model | Why |
|---|---|---|
| Draft SQL | `openai/gpt-oss-120b` | strongest free reasoning model; `reasoning_effort=medium` |
| Route + summarize | `openai/gpt-oss-20b` | fast and cheap; `reasoning_effort=low` |

Both are configurable in `.env`. Every call uses JSON mode and temperature 0, with
`include_reasoning=false` so only the final JSON comes back. Groq retired Llama 3.x on its free
tier on 2026-08-16. At start-up the app lists the models your key can use and logs an error if a
configured model is missing. A missing model at request time returns `LLM_MODEL_NOT_FOUND`.

## Data model (app database)

```
users(id, email UNIQUE, name, password_hash, created_at)
sessions(token_hash PK, user_id, csrf_token, created_at, expires_at)
datasets(id, user_id, name, db_file, meta_json, created_at)
query_log(request_id, user_id, dataset_id, conversation_id, question, sql, status, codes, created_at)
```

Each dataset is its own SQLite file under `data/users/<user_id>/<dataset_id>.db`, so users are
isolated at the file level as well as through ownership-checked lookups.

## Follow-up questions

Each chat carries a `conversation_id`. The last N successful (question, validated SQL) pairs are
fetched from `query_log` and given to the model as fenced `<history>`, so "what about 2024?" works.

## Production infrastructure

```mermaid
flowchart LR
    U[Users] -->|HTTPS| C[Caddy<br/>TLS, HSTS, LB]
    C --> A1[app replica 1]
    C --> A2[app replica N]
    A1 & A2 --> V[(appdata volume<br/>app.db WAL + datasets)]
    A1 & A2 -->|OTLP/HTTP| J[Jaeger]
    P[Prometheus] -->|/metrics bearer| A1 & A2
    A1 & A2 -->|JSON mode| G[Groq API]
```

| Concern | Implementation |
|---|---|
| Logs | JSON lines with `request_id` (`app/observability/logs.py`) |
| Metrics | dependency-free Prometheus registry (`app/observability/metrics.py`), `GET /metrics` |
| Traces | OpenTelemetry API spans per agent node + LLM call; SDK + OTLP exporter in the image |
| Usage | per-request LLM calls/tokens via contextvars, stored in `query_log`, returned as `usage` |
| Migrations | versioned, append-only, `BEGIN IMMEDIATE` so concurrent replicas migrate once |
| Rate limits | sliding window stored in the app DB, shared by every worker/replica on the host |
| Health | `/health` (liveness), `/ready` (readiness), Docker `HEALTHCHECK` |
| Evals | `evals/`: execution-accuracy golden set + red-team suite with a leak detector |
| CI/CD | ruff, mypy, pytest (3.12/3.13), red-team eval, Groq accuracy eval, pip-audit, gitleaks, image build + Trivy scan, push to GHCR |
