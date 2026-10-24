"""LLM clients.

* :class:`GroqClient`: Groq's OpenAI-compatible Chat Completions API in JSON mode.
* :class:`MockLLM`: deterministic offline model for demos without a key.
* :class:`ScriptedLLM`: replays canned responses per purpose (tests).

Every client returns a parsed JSON object or raises :class:`LLMError`. The
agent treats all output as an untrusted *candidate*.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal, Protocol

import httpx

log = logging.getLogger(__name__)

Purpose = Literal["route", "sql", "summary"]


class LLMError(Exception):
    def __init__(
        self, kind: Literal["rate_limited", "unavailable", "bad_output", "auth", "model_not_found"], message: str
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class LLMClient(Protocol):
    name: str

    def complete_json(
        self, *, purpose: Purpose, model: str, system: str, user: str, max_tokens: int = 700,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]: ...


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any]:
    match = _JSON_OBJECT.search(text or "")
    if not match:
        raise LLMError("bad_output", "model did not return a JSON object")
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise LLMError("bad_output", "model returned malformed JSON") from None
    if not isinstance(value, dict):
        raise LLMError("bad_output", "model returned a non-object JSON value")
    return value


class GroqClient:
    """Minimal Groq client (no SDK). Retries 429/5xx with bounded backoff."""

    name = "groq"

    def __init__(
        self, api_key: str, base_url: str, timeout_s: float = 30.0, max_retries: int = 2,
        transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
        on_usage: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            transport=transport,
        )
        self._max_retries = max_retries
        self._sleep = sleep
        self._on_usage = on_usage

    def complete_json(
        self, *, purpose: Purpose, model: str, system: str, user: str, max_tokens: int = 700,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if is_reasoning_model(model):
            # GPT-OSS reasons before answering; keep the reasoning out of the reply and its budget small.
            payload["reasoning_effort"] = reasoning_effort or "low"
            payload["include_reasoning"] = False
        attempt = 0
        while True:
            try:
                resp = self._client.post("/chat/completions", json=payload)
            except httpx.HTTPError:
                if attempt < self._max_retries:
                    attempt += 1
                    self._sleep(min(2**attempt, 8))
                    continue
                raise LLMError("unavailable", "the language model is unreachable") from None

            if resp.status_code == 200:
                try:
                    body = resp.json()
                    content = body["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError):
                    raise LLMError("bad_output", "unexpected response from the language model") from None
                usage = body.get("usage") if isinstance(body, dict) else None
                if self._on_usage and isinstance(usage, dict):
                    self._on_usage(model, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
                return parse_json_object(content)
            if resp.status_code in (401, 403):
                raise LLMError("auth", "the language model rejected the API key (check GROQ_API_KEY)")
            if resp.status_code == 404:
                log.error("groq model not found model=%s detail=%s", model, _error_detail(resp))
                raise LLMError(
                    "model_not_found",
                    f"the model '{model}' is not available on Groq; set SQLY_GROQ_SQL_MODEL / "
                    "SQLY_GROQ_FAST_MODEL in .env to a current model (see console.groq.com/docs/models)",
                )
            if resp.status_code == 400 and "json" in resp.text.lower():
                # JSON mode validation failure on the provider side: treat as bad output so we can retry/correct.
                raise LLMError("bad_output", "model failed to produce valid JSON")
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                attempt += 1
                retry_after = resp.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 2**attempt
                self._sleep(min(delay, 10.0))
                continue
            if resp.status_code == 429:
                raise LLMError("rate_limited", "the free-tier rate limit was reached; try again in a minute")
            log.warning("groq error status=%s purpose=%s detail=%s", resp.status_code, purpose, _error_detail(resp))
            raise LLMError("unavailable", "the language model returned an error")


    def available_models(self) -> set[str]:
        """Model ids this key can use (GET /models). Used for a start-up sanity check."""
        try:
            resp = self._client.get("/models")
            resp.raise_for_status()
            return {m["id"] for m in resp.json().get("data", []) if isinstance(m, dict) and "id" in m}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return set()


def is_reasoning_model(model: str) -> bool:
    return model.startswith("openai/gpt-oss")


def _error_detail(resp: httpx.Response) -> str:
    """Provider error message for logs (never contains the API key), truncated."""
    try:
        detail = resp.json().get("error", {})
        text = detail.get("message", "") if isinstance(detail, dict) else str(detail)
    except ValueError:
        text = resp.text
    return "".join(ch for ch in str(text) if ch.isprintable())[:300]


class ScriptedLLM:
    """Test double: returns canned dicts (or raises canned errors) per purpose."""

    name = "scripted"

    def __init__(self, script: Mapping[str, Iterable[dict[str, Any] | LLMError]]) -> None:
        self._script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict[str, str]] = []

    def complete_json(
        self, *, purpose: Purpose, model: str, system: str, user: str, max_tokens: int = 700,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"purpose": purpose, "model": model, "system": system, "user": user})
        queue = self._script.get(purpose)
        if not queue:
            raise LLMError("unavailable", f"no scripted response for {purpose}")
        item = queue.pop(0)
        if isinstance(item, LLMError):
            raise item
        return item


class MockLLM:
    """Offline stand-in. Understands a few demo questions on the sample dataset."""

    name = "mock"

    def complete_json(
        self, *, purpose: Purpose, model: str, system: str, user: str, max_tokens: int = 700,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        question = _between(user, "<user_question>", "</user_question>").lower()
        if purpose == "route":
            if re.search(r"\b(weather|joke|poem|capital of|who are you|recipe|news)\b", question):
                return {"intent": "out_of_scope", "reason": "not about the dataset"}
            if re.fullmatch(r"\s*(hi|hello|hey|thanks|thank you)[!. ]*", question):
                return {"intent": "chitchat", "reason": "greeting"}
            return {"intent": "data_question", "reason": "asks about the data"}
        if purpose == "summary":
            return {"answer": "Here are the results for your question."}
        return _mock_sql(question, user)


def _between(text: str, start: str, end: str) -> str:
    i, j = text.find(start), text.find(end)
    return text[i + len(start): j].strip() if 0 <= i < j else text


def _mock_sql(q: str, prompt: str) -> dict[str, Any]:
    has_shop = "TABLE customers" in prompt and "TABLE orders" in prompt
    if has_shop and "customer" in q and any(w in q for w in ("revenue", "spend", "top")):
        m = re.search(r"\btop\s+(\d{1,3})\b", q)
        n = max(1, min(int(m.group(1)) if m else 10, 100))
        return {
            "answerable": True,
            "sql": (
                "SELECT c.customer_id, c.name, ROUND(SUM(o.total_amount), 2) AS revenue "
                "FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
                "WHERE o.status <> 'cancelled' GROUP BY c.customer_id, c.name "
                f"ORDER BY revenue DESC LIMIT {n}"
            ),
            "explanation": f"Top {n} customers by non-cancelled order value.",
            "assumptions": ["cancelled orders are excluded from revenue"],
        }
    if has_shop and "month" in q:
        y = re.search(r"\b(20\d{2})\b", q)
        year = int(y.group(1)) if y else 2025
        return {
            "answerable": True,
            "sql": (
                "SELECT strftime('%Y-%m', o.order_date) AS month, COUNT(1) AS order_count, "
                "ROUND(SUM(o.total_amount), 2) AS sales FROM orders o "
                f"WHERE o.order_date >= '{year}-01-01' AND o.order_date < '{year + 1}-01-01' "
                "AND o.status <> 'cancelled' GROUP BY month ORDER BY month LIMIT 12"
            ),
            "explanation": f"Monthly non-cancelled sales for {year}.",
            "assumptions": [],
        }
    tables = re.findall(r"^TABLE (\w+)", prompt, flags=re.MULTILINE)
    if not tables:
        return {"answerable": False, "sql": None, "explanation": "no tables available", "assumptions": []}
    return {
        "answerable": True,
        "sql": f"SELECT COUNT(1) AS row_count FROM {tables[0]}",
        "explanation": f"(mock model) Row count of {tables[0]}. Set GROQ_API_KEY for real answers.",
        "assumptions": [],
    }


class InstrumentedLLM:
    """Wraps any client with metrics, tracing and per-request usage accounting."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.name = inner.name

    def complete_json(
        self, *, purpose: Purpose, model: str, system: str, user: str, max_tokens: int = 700,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        from app.observability import metrics
        from app.observability.context import current_stats
        from app.observability.tracing import set_attributes, span

        started = time.perf_counter()
        outcome = "ok"
        with span("llm.complete", purpose=purpose, model=model, provider=self.inner.name) as current:
            try:
                return self.inner.complete_json(
                    purpose=purpose, model=model, system=system, user=user, max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            except LLMError as err:
                outcome = err.kind
                set_attributes(current, error=err.kind)
                raise
            finally:
                elapsed = time.perf_counter() - started
                metrics.LLM_REQUESTS.inc(purpose=purpose, model=model, outcome=outcome)
                metrics.LLM_LATENCY.observe(elapsed, purpose=purpose, model=model)
                stats = current_stats()
                if stats is not None:
                    stats.add_call(model, elapsed * 1000, outcome == "ok")


def record_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """``GroqClient(on_usage=...)`` hook: token metrics + per-request totals."""
    from app.observability import metrics
    from app.observability.context import current_stats
    from app.observability.tracing import _otel_trace

    metrics.LLM_TOKENS.inc(prompt_tokens, model=model, type="prompt")
    metrics.LLM_TOKENS.inc(completion_tokens, model=model, type="completion")
    stats = current_stats()
    if stats is not None:
        stats.add_tokens(prompt_tokens, completion_tokens)
    if _otel_trace is not None:
        current = _otel_trace.get_current_span()
        current.set_attribute("sqly.prompt_tokens", prompt_tokens)
        current.set_attribute("sqly.completion_tokens", completion_tokens)
