"""SQL generator implementations.

* :class:`MockSQLGenerator`: deterministic, rule-based, offline. Default.
* :class:`ScriptedSQLGenerator`: replays a fixed list of candidates (tests).
* :class:`AnthropicSQLGenerator`: real model over HTTPS (stdlib only).

Swapping providers never touches the security layers: they all implement the
same :class:`SQLGenerator` protocol and only ever return a candidate.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable

from pydantic import ValidationError

from app.agent.llm_interface import (
    SYSTEM_PROMPT,
    GenerationError,
    GenerationRequest,
    SQLCandidate,
    render_user_prompt,
)


class MockSQLGenerator:
    """Keyword-routed deterministic generator covering the demo questions."""

    name = "mock"

    def generate(self, request: GenerationRequest) -> SQLCandidate:
        q = request.question.lower()
        n = _top_n(q, default=10, cap=request.max_rows)

        if "customer" in q and any(w in q for w in ("revenue", "spend", "spent", "top")):
            return SQLCandidate(
                sql=(
                    "SELECT c.customer_id, c.name, ROUND(SUM(o.total_amount), 2) AS revenue "
                    "FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
                    "WHERE o.status <> 'cancelled' "
                    "GROUP BY c.customer_id, c.name ORDER BY revenue DESC "
                    f"LIMIT {n}"
                ),
                explanation=f"Top {n} customers by total non-cancelled order value.",
            )
        if "month" in q and any(w in q for w in ("sales", "revenue", "orders")):
            year = _year(q) or 2025
            return SQLCandidate(
                sql=(
                    "SELECT strftime('%Y-%m', o.order_date) AS month, COUNT(*) AS orders, "
                    "ROUND(SUM(o.total_amount), 2) AS sales FROM orders o "
                    f"WHERE o.order_date >= '{year}-01-01' AND o.order_date < '{year + 1}-01-01' "
                    "AND o.status <> 'cancelled' GROUP BY month ORDER BY month LIMIT 12"
                ),
                explanation=f"Monthly non-cancelled sales for {year}.",
            )
        if "return" in q and "product" in q:
            return SQLCandidate(
                sql=(
                    "SELECT p.product_id, p.product_name, "
                    "ROUND(1.0 * SUM(CASE WHEN o.status = 'returned' THEN oi.quantity ELSE 0 END) "
                    "/ SUM(oi.quantity), 3) AS return_rate "
                    "FROM products p JOIN order_items oi ON oi.product_id = p.product_id "
                    "JOIN orders o ON o.order_id = oi.order_id "
                    "GROUP BY p.product_id, p.product_name ORDER BY return_rate DESC "
                    f"LIMIT {n}"
                ),
                explanation="Share of units sold that were in returned orders, per product.",
            )
        if "categor" in q:
            return SQLCandidate(
                sql=(
                    "SELECT p.category, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue "
                    "FROM products p JOIN order_items oi ON oi.product_id = p.product_id "
                    "GROUP BY p.category ORDER BY revenue DESC LIMIT 20"
                ),
                explanation="Gross line-item revenue per product category.",
            )
        if "city" in q or "cities" in q:
            return SQLCandidate(
                sql="SELECT c.city, COUNT(*) AS customers FROM customers c GROUP BY c.city ORDER BY customers DESC LIMIT 20",
                explanation="Number of customers per city.",
            )
        return SQLCandidate(
            sql="SELECT o.status, COUNT(*) AS orders FROM orders o GROUP BY o.status ORDER BY orders DESC LIMIT 10",
            explanation="Fallback: order counts by status (the mock model did not recognise the question).",
        )


def _top_n(q: str, default: int, cap: int) -> int:
    m = re.search(r"\b(?:top|first|best)\s+(\d{1,4})\b", q)
    value = int(m.group(1)) if m else default
    return max(1, min(value, cap))


def _year(q: str) -> int | None:
    m = re.search(r"\b(20\d{2})\b", q)
    return int(m.group(1)) if m else None


class ScriptedSQLGenerator:
    """Returns pre-baked candidates in order. Raises ``GenerationError`` for ``None`` entries."""

    name = "scripted"

    def __init__(self, responses: Iterable[SQLCandidate | str | None]) -> None:
        self._responses = list(responses)
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> SQLCandidate:
        self.requests.append(request)
        if not self._responses:
            raise GenerationError("script exhausted")
        item = self._responses.pop(0)
        if item is None:
            raise GenerationError("model returned unusable output")
        return item if isinstance(item, SQLCandidate) else SQLCandidate(sql=item, explanation="scripted")


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_candidate(text: str) -> SQLCandidate:
    """Parse the model's JSON answer into a candidate (tolerates code fences)."""
    match = _JSON_OBJECT.search(text or "")
    if not match:
        raise GenerationError("model response did not contain a JSON object")
    try:
        return SQLCandidate.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError):
        raise GenerationError("model response did not match the expected schema") from None


class AnthropicSQLGenerator:
    """Minimal Anthropic Messages API client using only the standard library."""

    name = "anthropic"
    _URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str, timeout_s: float = 30.0, max_tokens: int = 800) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s
        self._max_tokens = max_tokens

    def generate(self, request: GenerationRequest) -> SQLCandidate:
        body = json.dumps(
            {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "temperature": 0,
                "system": SYSTEM_PROMPT.format(max_rows=request.max_rows),
                "messages": [{"role": "user", "content": render_user_prompt(request)}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._URL,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 (fixed https URL)
                payload = json.loads(resp.read(1_000_000))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise GenerationError("model provider request failed") from None
        text = "".join(block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text")
        return parse_candidate(text)
