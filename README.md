# SQLYapper: chat with a SQL database, securely

Phase 1 is a small, secure vertical slice of an AI agent that answers natural-language
questions against a SQL database. The LLM proposes SQL. It is **never trusted**.
Every candidate passes several independent security layers before anything touches the database.

```
question ─► schema introspection ─► LLM candidate ─► SQLGlot AST ─► AST validator
         ─► re-serialised SQL ─► SQLite authorizer ─► bounded execution ─► structured result
                     ▲                                                     │
                     └──────── sanitised feedback (max 2 corrections) ◄────┘
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/SECURITY_POLICY.md`](docs/SECURITY_POLICY.md),
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) and [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) and the [test report](docs/TEST_REPORT.md).

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # optional; defaults are safe
python -m app.database.seed     # creates data/sqlyapper.db (deterministic)
uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs
```

## Try it

```bash
curl -s localhost:8000/health
curl -s localhost:8000/schema | python -m json.tool
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
     -d '{"question":"What are the top 10 customers by revenue?"}' | python -m json.tool
curl -s -X POST localhost:8000/validate -H 'content-type: application/json' \
     -d '{"sql":"SELECT * FROM customers LIMIT -1"}' | python -m json.tool
```

## Tests

```bash
pytest -v                                   # full suite
pytest -v --junitxml=reports/junit.xml      # machine-readable report
```

## Project layout

```
app/
  main.py               FastAPI app factory, middleware, error handlers
  config.py             immutable env-driven settings (SQLY_*)
  api/                  routes + request/response models
  database/             connection factory, seed, introspection, SQLite authorizer
  security/             policies (allowlists), limits, SQLGlot AST validator
  agent/                LLM interface, mock + Anthropic generators, correction loop
  execution/            constrained query executor
  tests/                adversarial unit + integration tests
docs/                   architecture, security policy, threat model, limitations, test report
```
