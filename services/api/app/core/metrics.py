"""Prometheus metrics of every process (api, gateway, worker).

The metrics are module-level singletons on the default registry: one process, one set of
numbers. Label values are kept to small, bounded sets (model, provider, task type, route
template): a label that grows with traffic (a request id, a raw URL path) would make the time
series explode.

Latency buckets are chosen for what is measured: the gateway's own overhead is sub-millisecond
to tens of milliseconds, a model answer takes tenths of a second to minutes.
"""

import time
from collections.abc import Iterable

from fastapi import FastAPI
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily, Metric
from prometheus_client.registry import Collector
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

NS = "switchyard"

HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
# A model answer: from a fast local model to a long generation.
ANSWER_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300)
# What the proxy itself adds before the request leaves for the provider.
OVERHEAD_BUCKETS = (0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25)
JOB_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

# ---- HTTP (api, gateway, worker health server) ------------------------------------------------
HTTP_REQUESTS = Counter(
    f"{NS}_http_requests_total",
    "HTTP requests handled, by route template and status.",
    ["service", "method", "route", "status"],
)
HTTP_DURATION = Histogram(
    f"{NS}_http_request_duration_seconds",
    "Time to handle an HTTP request (until the last byte was sent).",
    ["service", "method", "route"],
    buckets=HTTP_BUCKETS,
)

# ---- gateway ----------------------------------------------------------------------------------
GATEWAY_REQUESTS = Counter(
    f"{NS}_gateway_requests_total",
    "Chat requests finished by the gateway. (No task type label: a client may send any name.)",
    ["status", "mode", "provider", "model"],
)
GATEWAY_DURATION = Histogram(
    f"{NS}_gateway_request_duration_seconds",
    "Total time of a chat request, as the client saw it.",
    ["stream"],
    buckets=ANSWER_BUCKETS,
)
GATEWAY_TTFT = Histogram(
    f"{NS}_gateway_ttft_seconds",
    "Time from the start of the request to the first byte of the answer.",
    ["model"],
    buckets=ANSWER_BUCKETS,
)
GATEWAY_OVERHEAD = Histogram(
    f"{NS}_gateway_overhead_seconds",
    "Work the gateway does before the request leaves for the provider (its own cost).",
    buckets=OVERHEAD_BUCKETS,
)
GATEWAY_TOKENS = Counter(
    f"{NS}_gateway_tokens_total", "Tokens of finished requests.", ["direction", "model"]
)
GATEWAY_COST = Counter(
    f"{NS}_gateway_cost_usd_total", "Cost of finished requests in US dollars.", ["model"]
)
GATEWAY_IN_FLIGHT = Gauge(f"{NS}_gateway_in_flight_requests", "Chat requests being handled now.")
PROVIDER_ATTEMPTS = Counter(
    f"{NS}_provider_attempts_total",
    "Calls to a provider (a fallback makes a second one), by outcome.",
    ["provider", "model", "outcome"],
)
PROVIDER_LATENCY = Histogram(
    f"{NS}_provider_attempt_seconds",
    "Time of a provider call until the answer (or its first byte, for a stream).",
    ["provider", "model"],
    buckets=ANSWER_BUCKETS,
)
GATEWAY_FALLBACKS = Counter(
    f"{NS}_gateway_fallbacks_total",
    "Times the next model of the chain took over.",
    ["provider", "model", "reason"],
)
SNAPSHOT_AGE = Gauge(
    f"{NS}_snapshot_age_seconds", "Seconds since the gateway last confirmed its snapshot."
)
SNAPSHOT_REFRESHES = Counter(
    f"{NS}_snapshot_refreshes_total", "Snapshot refresh attempts.", ["result"]
)
LOG_EVENTS_DROPPED = Counter(
    f"{NS}_log_events_dropped_total",
    "Traffic events thrown away because the local buffer was full.",
)
LOG_EVENTS_BUFFERED = Gauge(
    f"{NS}_log_events_buffered", "Traffic events waiting in the local buffer."
)
CACHE_REQUESTS = Counter(
    f"{NS}_cache_requests_total", "Cache lookups.", ["cache", "result"]
)  # result: hit | miss

# ---- worker -----------------------------------------------------------------------------------
WORKER_MESSAGES = Counter(
    f"{NS}_worker_messages_total",
    "Messages handled by a consumer, by outcome (ok, retry, deferred, dead_letter).",
    ["queue", "outcome"],
)
WORKER_DURATION = Histogram(
    f"{NS}_worker_message_duration_seconds",
    "Time a consumer spent on one message.",
    ["queue"],
    buckets=JOB_BUCKETS,
)
QUEUE_MESSAGES = Gauge(
    f"{NS}_queue_messages", "Messages waiting in a queue (work, retry and dead-letter).", ["queue"]
)
QUEUE_CONSUMERS = Gauge(f"{NS}_queue_consumers", "Consumers of a queue.", ["queue"])
OUTBOX_PENDING = Gauge(f"{NS}_outbox_pending", "Outbox events not published yet.")
OUTBOX_OLDEST = Gauge(
    f"{NS}_outbox_oldest_pending_seconds", "Age of the oldest unpublished outbox event."
)
CALIBRATION_RUNS = Gauge(f"{NS}_calibration_runs", "Calibration runs by status.", ["status"])
CALIBRATION_PROGRESS = Gauge(
    f"{NS}_calibration_progress_ratio",
    "Share of the jobs of a running calibration that are done (0..1).",
    ["run_id", "task_type"],
)
REPLAY_RESULTS = Counter(
    f"{NS}_replay_results_total", "Calibration replay results stored, by status.", ["status"]
)


# ---- helpers ----------------------------------------------------------------------------------
class _PriceCacheCollector(Collector):
    """Hit and miss counts of the gateway's price cache (an `lru_cache`, which counts itself)."""

    def collect(self) -> Iterable[Metric]:
        from app.modules.gateway.budget import price_cache_info

        info = price_cache_info()
        family = CounterMetricFamily(
            f"{NS}_price_cache_requests", "Price cache lookups.", labels=["result"]
        )
        family.add_metric(["hit"], info.hits)
        family.add_metric(["miss"], info.misses)
        yield family


_collectors_registered = False


def register_extra_collectors() -> None:
    """Add the collectors that read other objects at scrape time (once per process)."""
    global _collectors_registered
    if not _collectors_registered:
        _collectors_registered = True
        REGISTRY.register(_PriceCacheCollector())


def render() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def sample(name: str, labels: dict[str, str] | None = None) -> float:
    """The current value of one series (tests and diagnostics); 0.0 if it does not exist yet."""
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else float(value)


def _route_of(scope: Scope) -> str:
    """The route template ('/projects/{project_id}/keys'), never the raw path, so that ids in
    URLs do not create a series each. Unmatched requests share one label."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class MetricsMiddleware:
    """Counts requests and times them. Pure ASGI (like the correlation-id middleware): it does
    not wrap the body, so streaming responses are untouched."""

    def __init__(self, app: ASGIApp, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = _route_of(scope)
            method = scope["method"]
            HTTP_REQUESTS.labels(self.service, method, route, str(status)).inc()
            HTTP_DURATION.labels(self.service, method, route).observe(time.perf_counter() - started)


def add_metrics_endpoint(app: FastAPI, service: str) -> None:
    """Expose `/metrics` and count the app's requests."""
    app.add_middleware(MetricsMiddleware, service=service)
    register_extra_collectors()

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return render()
