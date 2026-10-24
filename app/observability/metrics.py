"""A small, dependency-free Prometheus metrics registry.

Implements counters and histograms with labels and renders the Prometheus text
exposition format (version 0.0.4). Label values are escaped and label sets are
bounded by construction (route templates, fixed codes, configured model names).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence

_DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: Sequence[str], values: Sequence[str], extra: str = "") -> str:
    parts = [f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        self.name = name
        self.help = help_text
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self.labelnames):
            raise ValueError(f"{self.name}: expected labels {self.labelnames}, got {sorted(labels)}")
        return tuple(str(labels[n])[:120] for n in self.labelnames)

    def render(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("counters only go up")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        lines = super().render()
        with self._lock:
            for key, val in sorted(self._values.items()):
                lines.append(f"{self.name}{_labels(self.labelnames, key)} {val:g}")
        return lines


class Histogram(_Metric):
    kind = "histogram"

    def __init__(
        self, name: str, help_text: str, labelnames: Sequence[str] = (), buckets: Sequence[float] = _DEFAULT_BUCKETS
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(sorted(buckets))
        self._data: dict[tuple[str, ...], tuple[list[int], float, int]] = {}

    def observe(self, value: float, **labels: str) -> None:
        if math.isnan(value):
            return
        key = self._key(labels)
        with self._lock:
            counts, total, n = self._data.get(key, ([0] * len(self.buckets), 0.0, 0))
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1
            self._data[key] = (counts, total + value, n + 1)

    def count(self, **labels: str) -> int:
        entry = self._data.get(self._key(labels))
        return entry[2] if entry else 0

    def render(self) -> list[str]:
        lines = super().render()
        with self._lock:
            for key, (counts, total, n) in sorted(self._data.items()):
                for bound, c in zip(self.buckets, counts, strict=True):
                    lines.append(f"{self.name}_bucket{_labels(self.labelnames, key, f'le=\"{bound:g}\"')} {c}")
                lines.append(f"{self.name}_bucket{_labels(self.labelnames, key, 'le=\"+Inf\"')} {n}")
                lines.append(f"{self.name}_sum{_labels(self.labelnames, key)} {total:g}")
                lines.append(f"{self.name}_count{_labels(self.labelnames, key)} {n}")
        return lines


class Registry:
    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}

    def register(self, metric: _Metric) -> _Metric:
        if metric.name in self._metrics:
            raise ValueError(f"duplicate metric {metric.name}")
        self._metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> Counter:
        return self.register(Counter(name, help_text, labelnames))  # type: ignore[return-value]

    def histogram(
        self, name: str, help_text: str, labelnames: Sequence[str] = (), buckets: Sequence[float] = _DEFAULT_BUCKETS
    ) -> Histogram:
        return self.register(Histogram(name, help_text, labelnames, buckets))  # type: ignore[return-value]

    def render(self) -> str:
        lines: list[str] = []
        for metric in self._metrics.values():
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

HTTP_REQUESTS = REGISTRY.counter("sqly_http_requests_total", "HTTP requests", ("method", "route", "status"))
HTTP_LATENCY = REGISTRY.histogram("sqly_http_request_duration_seconds", "HTTP request latency", ("method", "route"))
AGENT_OUTCOMES = REGISTRY.counter("sqly_agent_requests_total", "Agent runs by outcome", ("kind",))
AGENT_LATENCY = REGISTRY.histogram("sqly_agent_duration_seconds", "End-to-end agent latency", ())
AGENT_GENERATIONS = REGISTRY.histogram(
    "sqly_agent_generations", "SQL drafts per question (1 + corrections)", (), buckets=(1, 2, 3)
)
NODE_LATENCY = REGISTRY.histogram("sqly_agent_node_duration_seconds", "Agent node latency", ("node",))
INPUT_BLOCKED = REGISTRY.counter("sqly_input_blocked_total", "Questions blocked by the input guard", ("category",))
GUARD_REJECTIONS = REGISTRY.counter("sqly_guard_rejections_total", "SQL guard rejections by code", ("code",))
LLM_REQUESTS = REGISTRY.counter("sqly_llm_requests_total", "LLM calls", ("purpose", "model", "outcome"))
LLM_LATENCY = REGISTRY.histogram("sqly_llm_duration_seconds", "LLM call latency", ("purpose", "model"))
LLM_TOKENS = REGISTRY.counter("sqly_llm_tokens_total", "LLM tokens", ("model", "type"))
QUERY_LATENCY = REGISTRY.histogram("sqly_query_duration_seconds", "Validated query execution time", ())
QUERY_ERRORS = REGISTRY.counter("sqly_query_errors_total", "Query execution errors by code", ("code",))
AUTH_EVENTS = REGISTRY.counter("sqly_auth_events_total", "Authentication events", ("event",))
