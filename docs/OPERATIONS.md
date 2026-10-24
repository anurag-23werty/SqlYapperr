# Operations Runbook

## Deploy with Docker Compose

```bash
cp .env.example .env
# Required in .env:
#   GROQ_API_KEY=gsk_...
#   SQLY_SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
#   SQLY_METRICS_TOKEN=<random>          # if you run Prometheus
#   SITE_ADDRESS=sql.example.com         # your domain (DNS A record -> this host); "localhost" for local
docker compose up -d --build
docker compose ps                        # app should be "healthy"
```

Compose forces `SQLY_ENV=production`, secure cookies and JSON logs. In production the app
**refuses to start** without a strong `SQLY_SECRET_KEY`. Caddy terminates TLS (Let's Encrypt
for a real domain) and is the only public entry point. Port 8000 is never published.

### With observability

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" > deploy/metrics_token
# put the same value in .env as SQLY_METRICS_TOKEN, and set:
#   SQLY_OTEL_EXPORTER_ENDPOINT=http://jaeger:4318
docker compose --profile observability up -d --build
```

- Prometheus: http://127.0.0.1:9090 (bound to localhost only)
- Jaeger traces: http://127.0.0.1:16686, service `sqlyapper`
- Example alerts: `deploy/alerts.example.yml`

### Scaling

Run **one worker per container** and scale with replicas:

```bash
docker compose up -d --scale app=3
```

Caddy and Prometheus discover replicas through Docker DNS. Metrics are per process, which is why
we scale containers rather than workers. Replicas share the `appdata` volume, so the SQLite app
database (WAL mode), sessions and the shared rate limiter work across replicas **on one host**.
Multi-host needs Postgres and Redis (Phase C).

## Health endpoints

| Endpoint | Purpose | Public via Caddy |
|---|---|---|
| `GET /health` | liveness: process is up | yes |
| `GET /ready` | readiness: app DB writable + migrated, sample DB present, LLM configured | no (404) |
| `GET /metrics` | Prometheus metrics; needs `Authorization: Bearer $SQLY_METRICS_TOKEN` | no (404) |

## Key metrics

| Metric | Watch for |
|---|---|
| `sqly_http_requests_total{route,status}` | 5xx rate |
| `sqly_agent_requests_total{kind}` | rising `rejected` / `error` share |
| `sqly_agent_duration_seconds` | p95 latency |
| `sqly_agent_generations` | more corrections = prompt/model regression |
| `sqly_guard_rejections_total{code}` | which rules the model keeps breaking |
| `sqly_input_blocked_total{category}` | injection attempts |
| `sqly_llm_requests_total{outcome}` | `rate_limited`, `model_not_found`, `unavailable` |
| `sqly_llm_tokens_total{model,type}` | token spend (free-tier quota) |
| `sqly_query_errors_total{code}` | **any `AUTHORIZATION_DENIED` is a security event** |
| `sqly_auth_events_total{event}` | sign-in failures / rate limiting |

Per-request usage (latency, LLM calls, tokens, drafts) is also stored in `query_log` in the app
database and returned to the client in `usage`. Users can see their last 24 hours at `GET /api/usage`.

## Logs

JSON lines on stdout, one access line per request (`logger=app.access`, route template, status,
duration) plus application events. Every line has a `request_id`, which is also returned to
clients in the `X-Request-ID` header and shown in the UI, so a user's report maps straight to logs and traces.
Prompts, result rows and hidden values are never logged.

```bash
docker compose logs -f app | jq 'select(.level != "INFO")'
docker compose logs app | jq 'select(.request_id == "<id>")'
```

## Traces

Each question produces one trace: `agent.run` → `agent.guard_input` → `agent.route` →
`agent.generate_sql` → `agent.guard_sql` → `agent.execute` → `agent.summarize`, with
`llm.complete` child spans (model, tokens, outcome). Spans carry codes and timings only.

## Backups

All state lives in the `appdata` volume: `app.db` (users, sessions, datasets, query log),
`sample.db`, and `users/<id>/<dataset>.db`.

```bash
docker compose exec app python -c "import sqlite3; s=sqlite3.connect('/data/app.db'); d=sqlite3.connect('/data/app-backup.db'); s.backup(d)"
docker run --rm -v sqlyapper_appdata:/data -v "$PWD":/out alpine tar czf /out/sqlyapper-$(date +%F).tgz -C /data .
```

Use SQLite's online backup API, as above, for `app.db`: copying a live WAL database file is not safe.

## Routine tasks

- **Groq model retired** (`sqly_llm_requests_total{outcome="model_not_found"}`, or a start-up log
  `Groq model '...' is not available`): pick a current model at console.groq.com/docs/models,
  update `SQLY_GROQ_*_MODEL`, run `make eval`, and redeploy if accuracy holds.
- **Rotate `SQLY_SECRET_KEY`**: change it and restart. All sessions become invalid, so users sign in again.
- **Rotate `GROQ_API_KEY` / `SQLY_METRICS_TOKEN`**: change it and restart (and update `deploy/metrics_token`).
- **Schema migrations** run automatically at start-up (versioned, safe with several replicas).
- **Authorizer denied guard-approved SQL** (alert): find the `request_id` in logs, read the trace,
  reproduce with `POST /api/datasets/{id}/validate`, and add the case to `evals/cases/redteam.jsonl`
  and the guard tests.
