from __future__ import annotations

import json

import httpx
import pytest

from app.agent.llm import GroqClient, LLMError, MockLLM, parse_json_object


def client(handler, retries: int = 2) -> GroqClient:  # type: ignore[no-untyped-def]
    return GroqClient("gsk_test", "https://api.groq.com/openai/v1", max_retries=retries,
                      transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_request_shape_uses_json_mode_and_bearer_key() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return ok('{"intent": "data_question"}')

    out = client(handler).complete_json(purpose="route", model="llama-3.1-8b-instant", system="s", user="u")
    assert out == {"intent": "data_question"}
    assert seen["url"].endswith("/openai/v1/chat/completions")
    assert seen["auth"] == "Bearer gsk_test"
    body = seen["body"]
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    assert body["model"] == "llama-3.1-8b-instant" and body["messages"][0]["role"] == "system"


def test_retries_on_429_then_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": "rate"})
        return ok('{"answer": "fine"}')

    assert client(handler).complete_json(purpose="summary", model="m", system="s", user="u") == {"answer": "fine"}
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("status", "kind"), [(429, "rate_limited"), (401, "auth"), (503, "unavailable"), (500, "unavailable")]
)
def test_error_mapping(status: int, kind: str) -> None:
    with pytest.raises(LLMError) as info:
        client(lambda r: httpx.Response(status, json={"error": "x"}), retries=0).complete_json(
            purpose="sql", model="m", system="s", user="u")
    assert info.value.kind == kind and "gsk_test" not in info.value.message


def test_network_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(LLMError) as info:
        client(handler, retries=1).complete_json(purpose="sql", model="m", system="s", user="u")
    assert info.value.kind == "unavailable"


@pytest.mark.parametrize("text", ["", "no json", "[1, 2]", '{"a": '])
def test_bad_output(text: str) -> None:
    with pytest.raises(LLMError) as info:
        parse_json_object(text)
    assert info.value.kind == "bad_output"


def test_mock_llm_routes_and_drafts() -> None:
    m = MockLLM()
    assert m.complete_json(purpose="route", model="x", system="", user="<user_question>what's the weather</user_question>")[
        "intent"] == "out_of_scope"
    out = m.complete_json(purpose="sql", model="x", system="", user="<schema>\nTABLE sales (a)\n</schema>\n<user_question>x</user_question>")
    assert out["sql"] == "SELECT COUNT(1) AS row_count FROM sales"


def test_gpt_oss_gets_reasoning_params_and_other_models_do_not() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return ok('{"ok": true}')

    c = client(handler)
    c.complete_json(purpose="sql", model="openai/gpt-oss-120b", system="s", user="u", reasoning_effort="medium")
    c.complete_json(purpose="route", model="openai/gpt-oss-20b", system="s", user="u")
    c.complete_json(purpose="route", model="some/other-model", system="s", user="u")
    assert bodies[0]["reasoning_effort"] == "medium" and bodies[0]["include_reasoning"] is False
    assert bodies[1]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in bodies[2] and "include_reasoning" not in bodies[2]
    assert all("max_completion_tokens" in b for b in bodies)


def test_unknown_model_gives_actionable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "The model `llama-3.3-70b-versatile` does not exist"}})

    with pytest.raises(LLMError) as info:
        client(handler).complete_json(purpose="sql", model="llama-3.3-70b-versatile", system="s", user="u")
    assert info.value.kind == "model_not_found"
    assert "llama-3.3-70b-versatile" in info.value.message and "SQLY_GROQ_SQL_MODEL" in info.value.message


def test_available_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}, {"id": "openai/gpt-oss-20b"}]})

    assert client(handler).available_models() == {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}
    assert client(lambda r: httpx.Response(500)).available_models() == set()


def test_default_models_are_current() -> None:
    from app.config import Settings

    s = Settings(llm_provider="mock")
    assert s.groq_sql_model == "openai/gpt-oss-120b" and s.groq_fast_model == "openai/gpt-oss-20b"
