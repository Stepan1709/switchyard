"""OpenTelemetry tracing: `client -> gateway -> provider` and `api -> RabbitMQ -> worker`.

Off by default (`OTEL_ENABLED=false`): without a configured provider every call here is a
no-op, so the hot path pays nothing. Turned on (profile `tracing`) it exports spans over OTLP
to Jaeger.

Spans come from two places:
- the FastAPI and httpx instrumentations: a server span for each request and a client span for
  each call to a provider;
- the messaging code in this repository: a message carries the W3C `traceparent` in its AMQP
  headers, and the consumer continues that trace. An event written to the outbox keeps the
  traceparent of the request that created it, so the trace survives the wait in the table and
  the relay's publish.
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)
TRACEPARENT = "traceparent"
_tracer = trace.get_tracer("switchyard")
_configured = False


def setup(settings: Settings, app: Any | None = None) -> bool:
    """Install the exporter and the instrumentations. Returns whether tracing is on."""
    global _configured
    if not settings.otel_enabled:
        return False
    # Health checks and scrapes arrive every few seconds and would drown the real traces.
    os.environ.setdefault("OTEL_PYTHON_EXCLUDED_URLS", "health/.*,metrics")
    if not _configured:
        # Heavy imports only when tracing is wanted.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

        provider = TracerProvider(
            resource=Resource.create({"service.name": f"switchyard-{settings.service_name}"}),
            sampler=ParentBasedTraceIdRatio(settings.otel_sample_ratio),
        )
        endpoint = settings.otel_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _configured = True
        log.info("tracing_enabled", endpoint=settings.otel_endpoint)
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health/.*,metrics")
    return True


def shutdown() -> None:
    """Send the spans that are still buffered (a short-lived process would lose them)."""
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if flush is not None:
        flush(timeout_millis=3000)


# ---- context carried by messages --------------------------------------------------------------
def current_traceparent() -> str | None:
    """The traceparent of the span that is active now, if there is a valid one."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get(TRACEPARENT)


def trace_headers() -> dict[str, str]:
    """Headers for a message: the active trace, or nothing."""
    value = current_traceparent()
    return {TRACEPARENT: value} if value else {}


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def context_from(headers: Mapping[str, Any]) -> otel_context.Context:
    carrier = {k: _text(v) for k, v in headers.items() if k in (TRACEPARENT, "tracestate")}
    return propagate.extract(carrier)


@contextmanager
def consumer_span(queue: str, message_type: str, headers: Mapping[str, Any]) -> Iterator[Span]:
    """Handle a message inside a span that continues the trace of whoever sent it."""
    with _tracer.start_as_current_span(
        f"process {queue}",
        context=context_from(headers),
        kind=SpanKind.CONSUMER,
        attributes={"messaging.system": "rabbitmq", "messaging.destination.name": queue,
                    "messaging.message.type": message_type},
    ) as span:  # fmt: skip
        yield span


@contextmanager
def producer_span(name: str, headers: Mapping[str, Any] | None = None) -> Iterator[Span]:
    """A span around a publish; continues the trace stored with the message, if any."""
    ctx = context_from(headers) if headers else None
    with _tracer.start_as_current_span(name, context=ctx, kind=SpanKind.PRODUCER) as span:
        yield span


def mark_error(span: Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def trace_ids() -> dict[str, str]:
    """trace_id and span_id of the active span, for log lines."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
