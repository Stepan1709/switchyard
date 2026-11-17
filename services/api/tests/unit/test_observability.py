"""Metrics and tracing helpers that need no containers."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from opentelemetry import trace

from app.core import metrics, tracing
from app.core.logging import _add_trace_context
from app.core.messaging.envelope import Message
from app.modules.gateway.record import RequestRecord
from app.modules.gateway.telemetry import MetricsRecorder
from app.worker.metrics_job import queue_names
from tests.conftest import SPAN_EXPORTER as _EXPORTER


@pytest.fixture(autouse=True)
def clean_spans():
    _EXPORTER.clear()


def value(name: str, **labels: str) -> float:
    return metrics.sample(name, labels)


# ---- HTTP middleware --------------------------------------------------------------------------
def app_under_test() -> FastAPI:
    app = FastAPI()
    metrics.add_metrics_endpoint(app, "unit")

    @app.get("/things/{thing_id}")
    async def thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    @app.get("/broken")
    async def broken() -> dict[str, str]:
        raise HTTPException(status_code=503, detail="down")

    @app.get("/crash")
    async def crash() -> dict[str, str]:
        raise RuntimeError("boom")

    return app


async def test_requests_are_counted_by_route_template_not_by_raw_path():
    app = app_under_test()
    before = value(
        "switchyard_http_requests_total",
        service="unit", method="GET", route="/things/{thing_id}", status="200",
    )  # fmt: skip
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for n in range(5):
            assert (await c.get(f"/things/{n}")).status_code == 200
    after = value(
        "switchyard_http_requests_total",
        service="unit", method="GET", route="/things/{thing_id}", status="200",
    )  # fmt: skip
    assert after - before == 5  # five different paths, one series


async def test_unmatched_paths_share_one_label_and_errors_keep_their_status():
    app = app_under_test()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        await c.get("/nothing-here-1")
        await c.get("/nothing-here-2")
        await c.get("/broken")
        crashed = await c.get("/crash")
    assert crashed.status_code == 500
    assert (
        value(
            "switchyard_http_requests_total",
            service="unit",
            method="GET",
            route="unmatched",
            status="404",
        )
        >= 2
    )
    assert (
        value(
            "switchyard_http_requests_total",
            service="unit",
            method="GET",
            route="/broken",
            status="503",
        )
        == 1
    )
    assert (
        value(
            "switchyard_http_requests_total",
            service="unit",
            method="GET",
            route="/crash",
            status="500",
        )
        == 1
    )


async def test_the_metrics_endpoint_serves_prometheus_text_and_is_not_counted():
    app = app_under_test()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "switchyard_http_requests_total" in r.text
    assert (
        value(
            "switchyard_http_requests_total",
            service="unit",
            method="GET",
            route="/metrics",
            status="200",
        )
        == 0
    )


# ---- the gateway's recorder -----------------------------------------------------------------
def finished(**over) -> RequestRecord:
    base = dict(
        request_id="r1", started_at=datetime.now(UTC), status="ok", mode="route", provider="p-obs",
        model="m-obs", prompt_tokens=100, completion_tokens=20, ttft_ms=250.0, duration_ms=900.0,
        overhead_ms=1.5, stream=True,
    )  # fmt: skip
    return RequestRecord(**{**base, **over})


def test_a_finished_request_feeds_every_gateway_metric():
    from decimal import Decimal

    labels = dict(status="ok", mode="route", provider="p-obs", model="m-obs")
    before = value("switchyard_gateway_requests_total", **labels)
    tokens = value("switchyard_gateway_tokens_total", direction="prompt", model="m-obs")
    ttft = value("switchyard_gateway_ttft_seconds_count", model="m-obs")
    overhead = value("switchyard_gateway_overhead_seconds_count")
    MetricsRecorder().submit(finished(cost_usd=Decimal("0.0021")))
    assert value("switchyard_gateway_requests_total", **labels) == before + 1
    assert (
        value("switchyard_gateway_tokens_total", direction="prompt", model="m-obs") == tokens + 100
    )
    assert value("switchyard_gateway_tokens_total", direction="completion", model="m-obs") == 20
    assert value("switchyard_gateway_ttft_seconds_count", model="m-obs") == ttft + 1
    assert value("switchyard_gateway_overhead_seconds_count") == overhead + 1
    assert value("switchyard_gateway_cost_usd_total", model="m-obs") == pytest.approx(0.0021)


def test_an_unauthenticated_request_is_counted_without_a_model_label():
    before = value(
        "switchyard_gateway_requests_total",
        status="unauthorized",
        mode="record",
        provider="none",
        model="none",
    )
    MetricsRecorder().submit(
        finished(
            status="unauthorized",
            mode="record",
            provider=None,
            model=None,
            ttft_ms=None,
            overhead_ms=None,
        )
    )
    assert (
        value(
            "switchyard_gateway_requests_total",
            status="unauthorized",
            mode="record",
            provider="none",
            model="none",
        )
        == before + 1
    )


def test_the_task_type_is_not_a_label():
    # A client may send any task type name: as a label it would create a series per name.
    assert "task_type" not in metrics.GATEWAY_REQUESTS._labelnames


# ---- queue names ----------------------------------------------------------------------------
def test_every_work_queue_is_watched_with_its_retry_stages_and_dead_letter_queue():
    assert queue_names(["worker.a"], 2) == [
        "worker.a", "worker.a.retry.1", "worker.a.retry.2", "worker.a.dlq",
    ]  # fmt: skip


# ---- tracing --------------------------------------------------------------------------------
def test_a_message_published_inside_a_span_carries_its_trace():
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("request") as span:
        amqp = Message(type="t", payload={}).to_amqp()
        sent = span.get_span_context()
    header = str(amqp.headers[tracing.TRACEPARENT])
    assert header.split("-")[1] == format(sent.trace_id, "032x")


def test_a_message_that_has_a_trace_keeps_it_even_when_republished_elsewhere():
    original = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("some other request"):
        amqp = Message(type="t", payload={}, headers={tracing.TRACEPARENT: original}).to_amqp()
    assert amqp.headers[tracing.TRACEPARENT] == original  # a retry stays in the original trace


def test_a_consumer_span_continues_the_senders_trace():
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("api request") as parent:
        headers = tracing.trace_headers()
        parent_ctx = parent.get_span_context()
    with tracing.consumer_span("worker.q", "evt", headers):
        ids = tracing.trace_ids()
    assert ids["trace_id"] == format(parent_ctx.trace_id, "032x")
    spans = {s.name: s for s in _EXPORTER.get_finished_spans()}
    consumer = spans["process worker.q"]
    assert consumer.parent is not None and consumer.parent.span_id == parent_ctx.span_id
    assert consumer.attributes["messaging.destination.name"] == "worker.q"


def test_header_values_that_arrive_as_bytes_are_understood():
    headers = {tracing.TRACEPARENT: b"00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    with tracing.consumer_span("q", "evt", headers):
        assert tracing.trace_ids()["trace_id"] == "0af7651916cd43dd8448eb211c80319c"


def test_without_a_span_nothing_is_added_to_messages_or_logs():
    assert tracing.trace_headers() == {} and tracing.trace_ids() == {}
    assert _add_trace_context(None, "", {"event": "x"}) == {"event": "x"}


def test_log_lines_inside_a_span_carry_trace_id_next_to_correlation_id():
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("request") as span:
        line = _add_trace_context(None, "", {"event": "x"})
        expected = format(span.get_span_context().trace_id, "032x")
    assert line["trace_id"] == expected and len(line["span_id"]) == 16
    assert json.dumps(line)  # serialisable as a log line
