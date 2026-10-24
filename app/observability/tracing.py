"""OpenTelemetry tracing.

Instrumentation uses only ``opentelemetry-api``, which is a no-op until an SDK
is configured. :func:`setup_tracing` installs the SDK and an OTLP/HTTP exporter
when ``SQLY_OTEL_EXPORTER_ENDPOINT`` is set (e.g. Jaeger or an OTel Collector).
Spans never carry prompts, result rows or hidden schema, only ids, codes,
timings, model names and token counts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

_otel_trace: Any
_tracer: Any
try:  # the API is a declared dependency; guard anyway so the app never fails to start on it
    from opentelemetry import trace as _otel_trace

    _tracer = _otel_trace.get_tracer("sqlyapper")
except ImportError:  # pragma: no cover
    _otel_trace = None
    _tracer = None


_SAFE_TYPES = (str, bool, int, float)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    if _tracer is None:  # pragma: no cover
        yield None
        return
    with _tracer.start_as_current_span(name) as current:
        set_attributes(current, **attributes)
        yield current


def set_attributes(current: Any, **attributes: Any) -> None:
    if current is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = [str(v)[:200] for v in value][:20]
        elif not isinstance(value, _SAFE_TYPES):
            value = str(value)
        if isinstance(value, str):
            value = value[:500]
        current.set_attribute(f"sqly.{key}", value)


def setup_tracing(endpoint: str | None, service_name: str, environment: str) -> bool:
    """Configure the OTel SDK if an endpoint is set and the SDK is installed."""
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("SQLY_OTEL_EXPORTER_ENDPOINT is set but opentelemetry-sdk/exporter are not installed "
                    "(pip install -r requirements-otel.txt); tracing disabled")
        return False
    provider = TracerProvider(resource=Resource.create({
        "service.name": service_name, "deployment.environment": environment,
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    assert _otel_trace is not None
    _otel_trace.set_tracer_provider(provider)
    log.info("tracing enabled endpoint=%s", endpoint)
    return True
