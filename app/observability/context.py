"""Per-request context propagated with contextvars.

Starlette copies the context into the worker thread that runs sync endpoints,
so values set by the middleware are visible inside the agent and LLM client.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

request_id_var: ContextVar[str | None] = ContextVar("sqly_request_id", default=None)
_stats_var: ContextVar[RequestStats | None] = ContextVar("sqly_request_stats", default=None)


@dataclass
class RequestStats:
    """Aggregated LLM usage for one request (no prompt or data content)."""

    llm_calls: int = 0
    llm_errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_ms: float = 0.0
    models: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_call(self, model: str, ms: float, ok: bool) -> None:
        with self._lock:
            self.llm_calls += 1
            self.llm_errors += 0 if ok else 1
            self.llm_ms += ms
            self.models.add(model)

    def add_tokens(self, prompt: int, completion: int) -> None:
        with self._lock:
            self.prompt_tokens += max(0, prompt)
            self.completion_tokens += max(0, completion)

    def as_dict(self) -> dict[str, object]:
        return {
            "llm_calls": self.llm_calls,
            "llm_errors": self.llm_errors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "llm_ms": round(self.llm_ms, 1),
            "models": sorted(self.models),
        }


def current_request_id() -> str | None:
    return request_id_var.get()


def current_stats() -> RequestStats | None:
    return _stats_var.get()


@contextmanager
def request_scope(request_id: str) -> Iterator[RequestStats]:
    """Bind a request id and a fresh stats accumulator for the duration of a block."""
    stats = RequestStats()
    t1 = request_id_var.set(request_id)
    t2 = _stats_var.set(stats)
    try:
        yield stats
    finally:
        request_id_var.reset(t1)
        _stats_var.reset(t2)
