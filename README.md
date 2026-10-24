# SQLYapper

Upload your data, ask questions in plain English, and get answers backed by SQL you can read.
The model drafts SQL. **That SQL never reaches the database directly.** A guard parses it into a
syntax tree, checks every node against an allowlist, and prints fresh SQL from the validated tree.
Only that rewritten SQL runs, read-only, under a second database-level authorizer.

```
question ─► input guard ─► router ─► LLM drafts SQL ─► AST guard ─► fresh SQL from the tree
           (gpt-oss-20b) (gpt-oss-120b)        │           │
                                feedback ◄─────┘ rejected  ▼
                                                SQLite authorizer + limits ─► rows ─► summary
```

## Features

- **Sign up / sign in** with HttpOnly session cookies, CSRF tokens and rate-limited logins.
- **Bring your own data**: upload CSV files. Each file becomes a table. Types, dates and join keys
  are detected, and indexes are added for join and date columns.
- **Sensitive columns start hidden** (email, phone, password, address, ...). Hidden columns are never
  shown to the model and can't be queried. You can hide or show any column yourself.
- **LangGraph agent** that routes questions, drafts SQL with Groq (free tier), and corrects itself
  at most twice. It handles out-of-scope questions and follow-ups.
- **Guardrails** against prompt injection: input guard, delimiter fencing, a router, the AST guard,
  and the SQLite authorizer. No `*` ever reaches the database.
- **Measured**: a golden-question accuracy suite and a red-team suite, gated in CI.
- **Observable**: JSON logs with request ids, Prometheus metrics, OpenTelemetry traces per agent step,
  and per-question token usage.
- **Deployable**: non-root Docker image, Compose stack with automatic HTTPS, versioned migrations,
  and rate limits shared across workers.

## Run it

```bash
python3.12 -m venv .venv && source .venv/bin/activate     # Python 3.12+
pip install -r requirements-dev.txt
cp .env.example .env
# edit .env: set GROQ_API_KEY (free key: https://console.groq.com/keys) and SQLY_SECRET_KEY
python -m uvicorn app.main:app --reload --port 8010
```

Open http://127.0.0.1:8010, create an account, and try the sample shop dataset or upload your own
CSV files. Without a Groq key, set `SQLY_LLM_PROVIDER=mock` to try the app with a simple offline model.

## Quality gates

```bash
make check          # ruff + mypy + pytest + offline red-team eval (what CI runs)
make eval           # accuracy eval against Groq (needs GROQ_API_KEY)
```

## Deploy

```bash
docker compose up -d --build                              # app + automatic HTTPS (Caddy)
docker compose --profile observability up -d --build      # + Prometheus + Jaeger
docker compose up -d --scale app=3                        # scale out
```

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for secrets, health checks, metrics, logs, traces and backups.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Security policy](docs/SECURITY_POLICY.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Known limitations](docs/LIMITATIONS.md)
- [Operations runbook](docs/OPERATIONS.md)
- [Evaluation harness](docs/EVALS.md)
- [Test report](docs/TEST_REPORT.md)

## Layout

```
app/
  main.py, config.py, env.py, services.py   app factory, settings, .env loader, wiring
  auth/                                      passwords (scrypt), sessions + CSRF, rate limiting
  storage/                                   app database, CSV ingestion + dataset metadata
  agent/                                     Groq client, prompts, LangGraph agent
  security/                                  AST guard, LIMIT rules, policies, prompt guard
  database/                                  read-only connections, SQLite authorizer, sample seed
  execution/                                 constrained executor (deadline, row/byte caps)
  api/                                       HTTP routes, schemas, auth/CSRF dependencies
  observability/                             JSON logs, Prometheus metrics, OpenTelemetry tracing
  web/static/                                sign-in page + chat UI (no framework, strict CSP)
  tests/                                     unit, adversarial and end-to-end tests
evals/                                       golden accuracy set, red-team suite, scorer, runner
deploy/                                      Caddyfile, Prometheus config, alert examples
.github/                                     CI pipeline, Dependabot
Dockerfile, docker-compose.yml, Makefile
```
