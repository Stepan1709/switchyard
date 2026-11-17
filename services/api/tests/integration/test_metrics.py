"""Metrics and traces of the real pipeline: gateway, consumers, queues, the outbox."""

import httpx
import pytest
from asgi_lifespan import LifespanManager
from opentelemetry import trace

from app.core import metrics, tracing
from app.core.messaging import Consumer, ConsumerSpec, Message, PermanentError, TemporaryError
from app.core.messaging.topology import QueueNames, declare_work_queue
from app.core.outbox import OutboxRelay, enqueue
from app.gateway_main import create_app as create_gateway_app
from app.modules.gateway.telemetry import MetricsRecorder
from app.worker.metrics_job import collect_queue_depths, queue_names
from tests.integration.calibration_support import Env, build_env
from tests.integration.conftest import MockServer, queue_depth, wait_until
from tests.integration.gateway_support import GatewayHandle, chat_body
from tests.integration.routing_support import go_live
from tests.integration.test_auth_api import bearer, login

pytestmark = pytest.mark.integration


def value(name: str, **labels: str) -> float:
    return metrics.sample(name, labels)


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def env(client, admin, api_app, mock_llm: MockServer) -> Env:
    return await build_env(client, admin, api_app, mock_llm.base_url, requests=0)


@pytest.fixture
async def gw(app_settings, env):
    """A gateway whose only recorder is the metrics one: no traffic events, so nothing waits
    for the broker when it stops."""
    settings = app_settings.model_copy(
        update={"service_name": "gateway", "forward_header_prefixes": ["x-mock-"],
                "gateway_default_rpm": 1000}
    )  # fmt: skip
    app = create_gateway_app(settings, recorder=MetricsRecorder())
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            yield GatewayHandle(app, client, None)


async def ask(gw: GatewayHandle, env: Env, **headers):
    return await gw.client.post(
        "/v1/chat/completions",
        json=chat_body("great product"),
        headers={**gw.auth(env.stack.key), "x-task-type": "classification", **headers},
    )


# ---- the gateway ------------------------------------------------------------------------------
async def test_a_request_through_the_gateway_is_counted_timed_and_exposed(client, env, gw):
    labels = dict(status="ok", mode="record", provider="mock", model="mock-strong")
    before = value("switchyard_gateway_requests_total", **labels)
    overhead = value("switchyard_gateway_overhead_seconds_count")
    r = await ask(gw, env)
    assert r.status_code == 200
    assert value("switchyard_gateway_requests_total", **labels) == before + 1
    assert value("switchyard_gateway_overhead_seconds_count") == overhead + 1
    assert (
        value(
            "switchyard_provider_attempts_total", provider="mock", model="mock-strong", outcome="ok"
        )
        >= 1
    )
    assert value("switchyard_gateway_in_flight_requests") == 0  # nothing is left running

    text = (await gw.client.get("/metrics")).text
    assert "switchyard_gateway_requests_total" in text
    assert "switchyard_snapshot_age_seconds" in text
    assert "switchyard_log_events_buffered" in text


async def test_a_rejected_key_is_counted_as_unauthorized(env, gw):
    before = value(
        "switchyard_gateway_requests_total",
        status="unauthorized",
        mode="record",
        provider="none",
        model="none",
    )
    r = await gw.client.post(
        "/v1/chat/completions", json=chat_body(), headers={"authorization": "Bearer swy_nope_nope"}
    )
    assert r.status_code == 401
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
    assert value("switchyard_gateway_in_flight_requests") == 0


async def test_a_fallback_counts_the_failed_attempt_and_the_one_that_answered(client, env, gw):
    await go_live(client, env, "mid", "ref")
    await gw.refresh()
    failed = value(
        "switchyard_gateway_fallbacks_total",
        provider="mock",
        model="mock-mid",
        reason="upstream_unreachable",
    ) + value(
        "switchyard_gateway_fallbacks_total",
        provider="mock",
        model="mock-mid",
        reason="bad_status_or_answer",
    )
    answered = value(
        "switchyard_provider_attempts_total", provider="mock", model="mock-strong", outcome="ok"
    )
    r = await ask(gw, env, **{"x-mock-fail": "503", "x-mock-fail-models": "mock-mid"})
    assert r.status_code == 200 and r.headers["x-switchyard-model"] == "mock-strong"
    text = (await gw.client.get("/metrics")).text
    assert 'switchyard_gateway_fallbacks_total{model="mock-mid"' in text
    assert (
        value(
            "switchyard_provider_attempts_total", provider="mock", model="mock-strong", outcome="ok"
        )
        == answered + 1
    )
    total = sum(
        value("switchyard_gateway_fallbacks_total", provider="mock", model="mock-mid", reason=r_)
        for r_ in (
            "upstream_unreachable",
            "bad_status_or_answer",
            "upstream_error",
            "upstream_timeout",
        )
    )
    assert total >= 1 and failed >= 0


async def test_the_api_counts_requests_by_route_template(client, admin, env):
    route = "/projects/{project_id}/keys"
    before = value(
        "switchyard_http_requests_total", service="api", method="GET", route=route, status="200"
    )
    for _ in range(2):
        assert (
            await client.get(f"/projects/{env.project_id}/keys", headers=admin)
        ).status_code == 200
    assert (
        value(
            "switchyard_http_requests_total", service="api", method="GET", route=route, status="200"
        )
        == before + 2
    )
    assert "switchyard_http_request_duration_seconds_bucket" in (await client.get("/metrics")).text


# ---- consumers and queues ---------------------------------------------------------------------
def consumer_for(amqp, publisher, name, handler):
    spec = ConsumerSpec(name, [f"{name}.event"], handler)
    return Consumer(spec, amqp, publisher, retry_delays_ms=[100, 200], requeue_pause=0.05)


async def send(publisher, name):
    await publisher.publish_event(Message(type=f"{name}.event", payload={"n": 1}))


async def test_consumer_outcomes_and_durations_are_counted(amqp, publisher, queue_name):
    async def handler(message):
        if message.payload.get("bad"):
            raise PermanentError("poison")
        if message.payload.get("flaky") and message.attempt == 0:
            raise TemporaryError("try again")

    consumer = consumer_for(amqp, publisher, queue_name, handler)
    await consumer.start()
    for payload in ({"n": 1}, {"flaky": 1}, {"bad": 1}):
        await publisher.publish_event(Message(type=f"{queue_name}.event", payload=payload))
    await wait_until(
        lambda: (
            value("switchyard_worker_messages_total", queue=queue_name, outcome="dead_letter") == 1
            and value("switchyard_worker_messages_total", queue=queue_name, outcome="ok") == 2
        )
    )
    await consumer.stop(5)
    assert value("switchyard_worker_messages_total", queue=queue_name, outcome="retry") == 1
    assert (
        value("switchyard_worker_messages_total", queue=queue_name, outcome="ok") == 2
    )  # + the retried one
    assert value("switchyard_worker_message_duration_seconds_count", queue=queue_name) == 4


async def test_queue_depths_include_the_dead_letter_queue(amqp, publisher, queue_name):
    async def handler(message):
        raise PermanentError("always")

    names = queue_names([queue_name], 2)
    channel = await amqp.channel()
    await declare_work_queue(
        channel, queue_name, [f"{queue_name}.event"], retry_delays_ms=[100, 200]
    )
    # nobody consumes yet: the messages wait in the work queue
    for _ in range(3):
        await send(publisher, queue_name)
    await collect_queue_depths(amqp, names)
    assert value("switchyard_queue_messages", queue=queue_name) == 3
    assert value("switchyard_queue_messages", queue=QueueNames(queue_name).dlq) == 0

    consumer = consumer_for(amqp, publisher, queue_name, handler)
    await consumer.start()
    await wait_until(lambda: queue_depth(amqp, QueueNames(queue_name).dlq))
    await consumer.stop(5)
    await collect_queue_depths(amqp, names)
    assert value("switchyard_queue_messages", queue=QueueNames(queue_name).dlq) == 3
    assert value("switchyard_queue_messages", queue=queue_name) == 0
    await channel.close()


async def test_a_missing_queue_does_not_stop_the_collection(amqp):
    await collect_queue_depths(amqp, ["no.such.queue.at.all"])  # logs and carries on


# ---- traces across RabbitMQ and the outbox ------------------------------------------------------
async def test_a_trace_survives_the_outbox_the_broker_and_the_consumer(
    api_app, amqp, publisher, queue_name, span_exporter
):
    factory = api_app.state.resources.session_factory
    seen: list[str] = []

    async def handler(message):
        seen.append(tracing.trace_ids().get("trace_id", ""))

    consumer = consumer_for(amqp, publisher, queue_name, handler)
    await consumer.start()

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("POST /calibration-runs") as request_span:
        sent_trace = format(request_span.get_span_context().trace_id, "032x")
        async with factory() as s, s.begin():
            enqueue(s, f"{queue_name}.event", {"job": 1})
    # the request is over; the relay runs later, outside of any span
    relay = OutboxRelay(
        factory, publisher, poll_interval=0.05, batch_size=10, unroutable_attempts=3
    )
    assert await relay.relay_batch() == 1
    await wait_until(lambda: seen)
    await consumer.stop(5)

    assert seen == [sent_trace]
    names = {s.name for s in span_exporter.get_finished_spans()}
    assert {
        "POST /calibration-runs",
        f"publish {queue_name}.event",
        f"process {queue_name}",
    } <= names
    assert {s.context.trace_id for s in span_exporter.get_finished_spans()} == {int(sent_trace, 16)}


async def test_the_outbox_gauges_and_calibration_progress_are_read_from_the_database(
    client, admin, env, api_app
):
    from app.worker.metrics_job import collect_database_gauges

    factory = api_app.state.resources.session_factory
    async with factory() as s, s.begin():
        enqueue(s, "metrics.test.nobody", {"x": 1})
    await collect_database_gauges(factory)
    assert value("switchyard_outbox_pending") >= 1
    assert value("switchyard_outbox_oldest_pending_seconds") >= 0
    assert value("switchyard_calibration_runs", status="running") >= 0
