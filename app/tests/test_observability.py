from __future__ import annotations

import io
import json
import logging
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.graph import AgentDeps, run_agent
from app.agent.llm import GroqClient, InstrumentedLLM, ScriptedLLM, record_usage
from app.auth.ratelimit import SharedRateLimiter
from app.config import Settings
from app.main import create_app
from app.observability import metrics
from app.observability.context import current_stats, request_scope
from app.observability.logs import JsonFormatter
from app.observability.metrics import Registry
from app.storage import app_db as app_db_module
from app.storage.app_db import AppDB

PASSWORD = "Str0ngPassw0rd"


# ------------------------------------------------------------------ metrics


def test_registry_renders_prometheus_text() -> None:
    reg = Registry()
    c = reg.counter("t_total", "help text", ("route",))
    h = reg.histogram("t_seconds", "latency", ("route",), buckets=(0.1, 1.0))
    c.inc(route='/a"b')
    c.inc(2, route="/x")
    h.observe(0.05, route="/x")
    h.observe(5.0, route="/x")
    text = reg.render()
    assert "# TYPE t_total counter" in text
    assert 't_total{route="/a\\"b"} 1' in text and 't_total{route="/x"} 2' in text
    assert 't_seconds_bucket{route="/x",le="0.1"} 1' in text
    assert 't_seconds_bucket{route="/x",le="+Inf"} 2' in text and 't_seconds_count{route="/x"} 2' in text
    with pytest.raises(ValueError):
        c.inc(wrong="x")
    with pytest.raises(ValueError):
        c.inc(-1, route="/x")


# ------------------------------------------------------------------ logging


def test_json_logs_carry_request_id_and_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("sqly.test.json")
    logger.addHandler(handler)
    logger.setLevel("INFO")
    with request_scope("req123"):
        logger.info("hello", extra={"route": "/x", "status": 200})
    line = json.loads(stream.getvalue().strip())
    assert line["msg"] == "hello" and line["request_id"] == "req123"
    assert line["route"] == "/x" and line["status"] == 200 and line["level"] == "INFO"


# ------------------------------------------------------------------ usage


def test_groq_usage_flows_into_request_stats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}],
                                         "usage": {"prompt_tokens": 120, "completion_tokens": 30}})

    client = InstrumentedLLM(GroqClient("k", "https://api.groq.com/openai/v1", transport=httpx.MockTransport(handler),
                                        on_usage=record_usage))
    before = metrics.LLM_TOKENS.value(model="m1", type="prompt")
    with request_scope("r1") as stats:
        client.complete_json(purpose="sql", model="m1", system="s", user="u")
        client.complete_json(purpose="route", model="m1", system="s", user="u")
    assert stats.llm_calls == 2 and stats.prompt_tokens == 240 and stats.completion_tokens == 60
    assert metrics.LLM_TOKENS.value(model="m1", type="prompt") - before == 240
    assert current_stats() is None  # scope cleaned up


def test_agent_records_outcome_and_guard_metrics(sample_ctx, mock_settings) -> None:  # type: ignore[no-untyped-def]
    llm = InstrumentedLLM(ScriptedLLM({
        "route": [{"intent": "data_question"}],
        "sql": [{"answerable": True, "sql": "SELECT * FROM customers"},
                {"answerable": True, "sql": "SELECT c.city FROM customers c LIMIT 3"}],
        "summary": [{"answer": "ok"}],
    }))
    before_answer = metrics.AGENT_OUTCOMES.value(kind="answer")
    before_star = metrics.GUARD_REJECTIONS.value(code="STAR_NOT_ALLOWED")
    with request_scope("r2") as stats:
        st = run_agent(AgentDeps(llm, mock_settings, sample_ctx), "cities", use_langgraph=False)
    assert st["kind"] == "answer" and st["agent_ms"] > 0
    assert metrics.AGENT_OUTCOMES.value(kind="answer") == before_answer + 1
    assert metrics.GUARD_REJECTIONS.value(code="STAR_NOT_ALLOWED") == before_star + 1
    assert stats.llm_calls == 4  # route + 2 drafts + summary


def test_tracing_spans_cover_every_node_without_content(sample_ctx, mock_settings) -> None:  # type: ignore[no-untyped-def]
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    llm = InstrumentedLLM(ScriptedLLM({
        "route": [{"intent": "data_question"}],
        "sql": [{"answerable": True, "sql": "SELECT c.city FROM customers c LIMIT 3"}],
        "summary": [{"answer": "ok"}],
    }))
    run_agent(AgentDeps(llm, mock_settings, sample_ctx), "secret-question-text", use_langgraph=False)
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert {"agent.run", "agent.guard_input", "agent.route", "agent.generate_sql", "agent.guard_sql",
            "agent.execute", "agent.summarize", "llm.complete"} <= names
    dump = json.dumps([dict(s.attributes or {}) for s in spans])
    assert "secret-question-text" not in dump


# ------------------------------------------------------------------ HTTP


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(llm_provider="mock", data_dir=tmp_path, db_path=tmp_path / "s.db", app_db_path=tmp_path / "a.db")


def test_metrics_endpoint_access_rules(tmp_path: Path) -> None:
    base = dict(llm_provider="mock", data_dir=tmp_path, db_path=tmp_path / "s.db", app_db_path=tmp_path / "a.db")
    dev = TestClient(create_app(Settings(**base), use_langgraph=False))
    assert dev.get("/metrics").status_code == 200
    protected = TestClient(create_app(Settings(**base, metrics_token="t0ken"), use_langgraph=False))
    assert protected.get("/metrics").status_code == 401
    assert protected.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = protected.get("/metrics", headers={"Authorization": "Bearer t0ken"})
    assert r.status_code == 200 and "sqly_http_requests_total" in r.text
    prod = TestClient(create_app(Settings(**base, env="production", secret_key="s" * 40, cookie_secure=True),
                                 use_langgraph=False))
    assert prod.get("/metrics").status_code == 404


def test_ready_and_http_metrics(settings: Settings) -> None:
    client = TestClient(create_app(settings, use_langgraph=False))
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"
    before = metrics.HTTP_REQUESTS.value(method="GET", route="/api/datasets", status="401")
    client.get("/api/datasets")
    assert metrics.HTTP_REQUESTS.value(method="GET", route="/api/datasets", status="401") == before + 1
    unknown = metrics.HTTP_REQUESTS.value(method="GET", route="unmatched", status="404")
    client.get("/no/such/path/12345")
    assert metrics.HTTP_REQUESTS.value(method="GET", route="unmatched", status="404") == unknown + 1


def test_chat_returns_usage_and_logs_stats(settings: Settings) -> None:
    client = TestClient(create_app(settings, use_langgraph=False))
    csrf = client.post("/api/auth/signup", json={"email": "a@b.co", "name": "A", "password": PASSWORD}).json()["csrf_token"]
    ds = client.get("/api/datasets").json()[0]["id"]
    body = client.post(f"/api/datasets/{ds}/chat", json={"question": "What are the top 3 customers by revenue?"},
                       headers={"X-CSRF-Token": csrf}).json()
    assert body["kind"] == "answer" and body["usage"]["llm_calls"] == 3 and body["usage"]["generations"] == 1
    row = sqlite3.connect(settings.app_db_path).execute(
        "SELECT llm_calls, generations, latency_ms FROM query_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == 3 and row[1] == 1 and row[2] > 0
    usage = client.get("/api/usage").json()
    assert usage["questions"] == 1 and usage["answered"] == 1


# --------------------------------------------------------------- migrations


def test_legacy_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(app_db_module._SCHEMA_V1)
    conn.execute("INSERT INTO users VALUES ('u1', 'a@b.co', 'A', 'h', 0)")
    conn.commit()
    conn.close()
    db = AppDB(path)
    assert db.schema_version() == 2
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(query_log)")}
    assert {"latency_ms", "prompt_tokens", "generations"} <= cols
    assert sqlite3.connect(path).execute("SELECT COUNT(1) FROM users").fetchone()[0] == 1  # data kept
    assert AppDB(path).schema_version() == 2  # idempotent


def test_concurrent_startup_migrates_once(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    errors: list[BaseException] = []

    def start() -> None:
        try:
            AppDB(path)
        except BaseException as exc:  # noqa: BLE001  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rows = sqlite3.connect(path).execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert rows == [(1,), (2,)]


def test_shared_rate_limiter_spans_instances_and_hashes_keys(tmp_path: Path) -> None:
    path = tmp_path / "rl.db"
    a, b = SharedRateLimiter(AppDB(path), 3, 60, "login"), SharedRateLimiter(AppDB(path), 3, 60, "login")
    assert [a.allow("x@y.co"), b.allow("x@y.co"), a.allow("x@y.co")] == [True, True, True]
    assert not b.allow("x@y.co")  # the 4th hit is denied regardless of which worker sees it
    stored = sqlite3.connect(path).execute("SELECT DISTINCT key FROM rate_events").fetchall()
    assert stored and all("x@y.co" not in k for (k,) in stored)
    a.reset("x@y.co")
    assert b.allow("x@y.co")


def test_tracing_setup_is_optional() -> None:
    from app.observability.tracing import setup_tracing

    assert setup_tracing(None, "sqlyapper", "test") is False
    # With an endpoint but without the OTLP exporter installed, it must warn and carry on, never crash.
    try:
        import opentelemetry.exporter.otlp.proto.http  # noqa: F401
    except ImportError:
        assert setup_tracing("http://localhost:4318", "sqlyapper", "test") is False
