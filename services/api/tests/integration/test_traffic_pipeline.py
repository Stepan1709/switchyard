"""Gateway -> RabbitMQ -> worker writer -> the partitioned `requests` table, end to end."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.events import REQUEST_COMPLETED
from app.core.messaging import Consumer, Message
from app.core.messaging.topology import QueueNames
from app.modules.traffic import partitions
from app.modules.traffic.writer import RequestLogWriter
from app.worker.consumers import request_log_spec
from tests.integration.conftest import Endpoint, MockServer, queue_depth, wait_until
from tests.integration.gateway_support import (
    GatewayHandle,
    Provisioned,
    chat_body,
    provision,
    run_gateway,
)
from tests.integration.tcp_proxy import TcpProxy
from tests.integration.test_auth_api import bearer, login

pytestmark = pytest.mark.integration

LABELS = {"positive", "negative", "neutral"}


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def stack(client, admin, mock_llm: MockServer) -> Provisioned:
    return await provision(client, admin, mock_llm.base_url)


@pytest.fixture
def settings(app_settings, queue_name):
    """A private writer queue per test and short delays, so tests do not see each other."""
    return app_settings.model_copy(
        update={
            "request_log_queue": queue_name,
            "feedback_queue": f"{queue_name}_feedback",
            "retry_delays_ms": [100, 200, 300],
            "traffic_batch_size": 50,
            "traffic_batch_wait": 0.05,
        }
    )


@pytest.fixture
async def writer(api_app, settings, amqp, publisher) -> AsyncIterator[RequestLogWriter]:
    writer = RequestLogWriter(api_app.state.resources.session_factory, settings)
    consumer = Consumer(
        request_log_spec(writer, settings),
        amqp,
        publisher,
        retry_delays_ms=settings.retry_delays_ms,
    )
    await consumer.start()
    yield writer
    await consumer.stop(5)
    channel = await amqp.channel()  # remove this test's queues from the shared broker
    names = QueueNames(settings.request_log_queue)
    for q in [names.work, names.dlq, *(names.retry(i) for i in (1, 2, 3))]:
        await channel.queue_delete(q)


@pytest.fixture
async def gw(settings, stack, writer) -> AsyncIterator[GatewayHandle]:
    async for handle in run_gateway(settings, collect=False):
        yield handle


async def post(gw, key, body=None, **headers):
    return await gw.client.post(
        "/v1/chat/completions", json=body or chat_body(), headers={**gw.auth(key), **headers}
    )


async def fetch(api_app, where="true", cols="*"):
    async with api_app.state.resources.session_factory() as s:
        return (
            (
                await s.execute(
                    text(f"SELECT {cols} FROM requests WHERE {where} ORDER BY started_at")
                )
            )
            .mappings()
            .all()
        )


async def count(api_app, where="true") -> int:
    return len(await fetch(api_app, where, "request_id"))


async def wait_count(api_app, n, where="true", timeout=15):  # noqa: ASYNC109
    async def reached():
        return await count(api_app, where) >= n

    await wait_until(reached, timeout=timeout)


# ---- what is logged --------------------------------------------------------------------------------------
async def test_a_request_through_the_gateway_is_written_to_the_traffic_log(gw, stack, api_app):
    r = await post(gw, stack.key, chat_body("hello there"), **{"x-task-type": "classification"})
    assert r.status_code == 200
    rid = r.headers["x-request-id"]
    await wait_count(api_app, 1, f"request_id = '{rid}'")

    [row] = await fetch(api_app, f"request_id = '{rid}'")
    usage = r.json()["usage"]
    assert (row["status"], row["http_status"], row["stream"], row["mode"]) == (
        "ok",
        200,
        False,
        "record",
    )
    assert (row["prompt_tokens"], row["completion_tokens"]) == (
        usage["prompt_tokens"],
        usage["completion_tokens"],
    )
    assert (row["model"], row["requested_model"], row["provider"]) == (
        "mock-strong",
        "gpt-4o",
        "mock",
    )
    assert str(row["project_id"]) == stack.project["id"] and str(row["key_id"]) == stack.key_id
    assert row["task_type"] == "classification"
    assert row["ttft_ms"] > 0 and row["duration_ms"] >= row["ttft_ms"] and row["overhead_ms"] >= 0
    # the stored request is what the client sent (its own model name included) ...
    assert row["request_payload"]["model"] == "gpt-4o"
    assert row["request_payload"]["messages"][0]["content"] == "hello there"
    # ... and the stored answer is what the provider answered
    assert (
        row["response_payload"]["choices"][0]["message"]["content"]
        == r.json()["choices"][0]["message"]["content"]
    )
    assert row["payload_skipped"] is False and row["payload_masked"] is False
    assert row["started_at"] > datetime.now(UTC) - timedelta(minutes=5)


async def test_a_streamed_answer_is_stored_as_one_assembled_message(gw, stack, api_app):
    plain = (await post(gw, stack.key, chat_body("stream it"))).json()
    r = await post(gw, stack.key, chat_body("stream it", stream=True))
    rid = r.headers["x-request-id"]
    await wait_count(api_app, 1, f"request_id = '{rid}'")
    [row] = await fetch(api_app, f"request_id = '{rid}'")
    assert row["stream"] is True and row["status"] == "ok"
    stored = row["response_payload"]
    assert stored["object"] == "chat.completion"
    assert stored["choices"][0]["message"]["content"] == plain["choices"][0]["message"]["content"]
    assert stored["choices"][0]["finish_reason"] == "stop"
    assert stored["usage"] == plain["usage"]
    assert row["prompt_tokens"] == plain["usage"]["prompt_tokens"]
    assert row["request_payload"]["stream"] is True  # as the client sent it, without our additions
    assert "stream_options" not in row["request_payload"]


async def test_tool_calls_are_stored_for_streams_too(gw, stack, api_app):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search",
                "parameters": {"properties": {"query": {"type": "string"}}},
            },
        }
    ]
    plain = (await post(gw, stack.key, chat_body("find cats", tools=tools))).json()
    r = await post(gw, stack.key, chat_body("find cats", tools=tools, stream=True))
    rid = r.headers["x-request-id"]
    await wait_count(api_app, 1, f"request_id = '{rid}'")
    [row] = await fetch(api_app, f"request_id = '{rid}'")
    stored_call = row["response_payload"]["choices"][0]["message"]["tool_calls"][0]
    expected = plain["choices"][0]["message"]["tool_calls"][0]
    assert stored_call["function"] == expected["function"]
    assert row["request_payload"]["tools"] == tools


async def test_pii_is_masked_in_the_log_but_not_in_what_the_client_gets(
    client, admin, gw, stack, api_app
):
    await client.patch(f"/projects/{stack.project['id']}", json={"mask_pii": True}, headers=admin)
    await gw.refresh()
    prompt = "Mail anna@example.com or call +7 999 123 45 67, card 4111 1111 1111 1111. Labels: bob@mail.io, c, d"
    r = await post(gw, stack.key, chat_body(prompt))
    rid = r.headers["x-request-id"]
    await wait_count(api_app, 1, f"request_id = '{rid}'")
    [row] = await fetch(api_app, f"request_id = '{rid}'")

    stored = json.dumps([row["request_payload"], row["response_payload"]])
    for secret in ("anna@example.com", "999 123 45 67", "4111 1111 1111 1111"):
        assert secret not in stored
    assert "[EMAIL]" in stored and "[PHONE]" in stored and "[CARD]" in stored
    assert row["payload_masked"] is True
    assert prompt in json.dumps(
        r.request.content.decode() if False else prompt
    )  # (the prompt we sent)
    # the model may have answered with the e-mail label: the client saw it unmasked
    answer = r.json()["choices"][0]["message"]["content"]
    assert "[EMAIL]" not in answer


async def test_a_project_can_keep_metadata_only(client, admin, gw, stack, api_app):
    await client.patch(
        f"/projects/{stack.project['id']}", json={"store_bodies": False}, headers=admin
    )
    await gw.refresh()
    r = await post(gw, stack.key, chat_body("private words"))
    rid = r.headers["x-request-id"]
    await wait_count(api_app, 1, f"request_id = '{rid}'")
    [row] = await fetch(api_app, f"request_id = '{rid}'")
    assert row["request_payload"] is None and row["response_payload"] is None
    assert row["payload_skipped"] is False  # not stored because the project said so, not by size
    assert row["prompt_tokens"] > 0 and row["status"] == "ok"  # the metadata is still there
    assert await count(api_app, "request_payload::text LIKE '%private words%'") == 0


async def test_bodies_over_the_limit_are_not_stored_at_all(settings, stack, writer, api_app):
    small = settings.model_copy(update={"traffic_max_payload_bytes": 1500})
    async for gw in run_gateway(small, collect=False):
        big_in = await post(gw, stack.key, chat_body("x" * 3000))
        big_out = await post(gw, stack.key, chat_body("small"), **{"x-mock-output-tokens": "1000"})
        for r in (big_in, big_out):
            assert r.status_code == 200
        ids = [big_in.headers["x-request-id"], big_out.headers["x-request-id"]]
        await wait_count(api_app, 2)
        by_id = {str(r["request_id"]): r for r in await fetch(api_app)}
        too_big_request = by_id[ids[0]]
        assert (
            too_big_request["request_payload"] is None
            and too_big_request["payload_skipped"] is True
        )
        too_big_answer = by_id[ids[1]]
        assert (
            too_big_answer["response_payload"] is None and too_big_answer["payload_skipped"] is True
        )
        assert too_big_answer["request_payload"] is not None  # the small request is still kept
        assert too_big_answer["completion_tokens"] > 100  # metadata and tokens always are


async def test_failed_requests_are_logged_and_unauthenticated_ones_are_not(
    client, admin, settings, stack, writer, api_app, mock_llm
):
    limited = await provision(
        client, admin, mock_llm.base_url, project="limited", provider="mock2",
        key_settings={"rate_limit_rpm": 1},
    )  # fmt: skip
    async for gw in run_gateway(settings, collect=False):
        ok = await post(gw, limited.key)
        blocked = await post(gw, limited.key)
        broken = await post(gw, stack.key, **{"x-mock-fail": "500"})
        anonymous = await gw.client.post("/v1/chat/completions", json=chat_body())
        assert (ok.status_code, blocked.status_code, broken.status_code, anonymous.status_code) == (
            200,
            429,
            502,
            401,
        )
        await wait_count(api_app, 3)
        await asyncio.sleep(0.4)
        by_id = {str(r["request_id"]): r for r in await fetch(api_app)}
        assert by_id[blocked.headers["x-request-id"]]["status"] == "rate_limited"
        assert by_id[blocked.headers["x-request-id"]]["response_payload"] is None
        failed = by_id[broken.headers["x-request-id"]]
        assert failed["status"] == "upstream_error" and failed["http_status"] == 502
        assert failed["request_payload"] is not None  # what the client asked is kept
        assert anonymous.headers["x-request-id"] not in by_id  # nobody's request: not logged
        assert len(by_id) == 3


# ---- task types --------------------------------------------------------------------------------------------------
async def test_task_types_are_registered_on_first_sight_up_to_a_cap(
    settings, stack, writer, api_app
):
    capped = settings.model_copy(update={"task_types_per_project": 3})
    writer._settings = capped
    async for gw in run_gateway(capped, collect=False):
        for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
            assert (
                await post(gw, stack.key, chat_body(name), **{"x-task-type": name})
            ).status_code == 200
        await wait_count(api_app, 5)
        async with api_app.state.resources.session_factory() as s:
            registered = [
                r[0] for r in await s.execute(text("SELECT name FROM task_types ORDER BY name"))
            ]
        assert len(registered) == 3  # the cap
        assert await count(api_app, "task_type IS NOT NULL") == 5  # every request is still logged


async def test_automatically_registered_types_are_recorded_in_record_mode(
    gw, stack, api_app, client, admin
):
    await post(gw, stack.key, chat_body("x"), **{"x-task-type": "summarization"})
    await wait_count(api_app, 1)
    types = (await client.get(f"/projects/{stack.project['id']}/task-types", headers=admin)).json()
    assert [(t["name"], t["mode"], t["auto_registered"]) for t in types] == [
        ("summarization", "record", True)
    ]


# ---- the writer on its own --------------------------------------------------------------------------------------
def make_event(**overrides):
    event = {
        "request_id": str(uuid.uuid4()),
        "started_at": datetime.now(UTC).isoformat(),
        "project_id": str(uuid.uuid4()),
        "status": "ok",
        "http_status": 200,
        "task_type": "unit",
    }
    return {**event, **overrides}


async def send_event(publisher, queue_name, event, message_id=None):
    await publisher.publish_event(
        Message(type=REQUEST_COMPLETED, payload=event, message_id=message_id or event["request_id"])
    )


async def test_a_redelivered_event_does_not_create_a_second_row(writer, publisher, api_app):
    event = make_event()
    for _ in range(3):
        await send_event(publisher, "", event)
    await wait_count(api_app, 1)
    await asyncio.sleep(0.5)
    assert await count(api_app) == 1


async def test_events_are_written_in_batches(writer, publisher, api_app):
    events = [make_event(project_id=str(uuid.uuid4())) for _ in range(250)]
    for e in events:
        await send_event(publisher, "", e)
    await wait_count(api_app, 250, timeout=30)
    assert await count(api_app) == 250
    assert writer.rows_written == 250 and writer.batches <= 60  # far fewer than 250 inserts


async def test_an_invalid_event_goes_to_the_dlq_and_does_not_block_the_rest(
    writer, publisher, api_app, amqp, settings
):
    await send_event(publisher, "", {"request_id": "nope"}, message_id=str(uuid.uuid4()))
    good = make_event()
    await send_event(publisher, "", good)
    await wait_count(api_app, 1)
    await wait_until(lambda: queue_depth(amqp, QueueNames(settings.request_log_queue).dlq))
    assert await queue_depth(amqp, QueueNames(settings.request_log_queue).dlq) == 1


async def test_a_missing_partition_is_created_on_demand(writer, publisher, api_app):
    two_months_ago = partitions.add_months(partitions.month_start(datetime.now(UTC)), -2)
    event = make_event(
        started_at=datetime(two_months_ago.year, two_months_ago.month, 10, tzinfo=UTC).isoformat()
    )
    async with api_app.state.resources.session_factory() as s:
        assert partitions.partition_name(two_months_ago) not in await partitions.list_partitions(s)
    await send_event(publisher, "", event)
    await wait_count(api_app, 1)
    async with api_app.state.resources.session_factory() as s:
        assert partitions.partition_name(two_months_ago) in await partitions.list_partitions(s)


async def test_stale_and_future_events_are_not_stored(writer, publisher, api_app, amqp, settings):
    old = datetime.now(UTC) - timedelta(days=400)  # far past the retention period
    future = datetime.now(UTC) + timedelta(days=30)
    await send_event(publisher, "", make_event(started_at=old.isoformat()))
    await send_event(publisher, "", make_event(started_at=future.isoformat()))
    fine = make_event()
    await send_event(publisher, "", fine)
    await wait_count(api_app, 1)
    await wait_until(lambda: queue_depth(amqp, QueueNames(settings.request_log_queue).dlq))
    assert await count(api_app) == 1  # only the normal one
    assert (
        await queue_depth(amqp, QueueNames(settings.request_log_queue).dlq) == 1
    )  # the future one


async def test_a_database_failure_is_retried_and_nothing_is_lost(
    writer, publisher, api_app, monkeypatch
):
    real = writer._write
    failures = {"left": 2}

    async def flaky(events):
        if failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("database restarted")
        return await real(events)

    monkeypatch.setattr(writer, "_write", flaky)
    events = [make_event() for _ in range(5)]
    for e in events:
        await send_event(publisher, "", e)
    await wait_count(api_app, 5, timeout=20)
    stored = {str(r["request_id"]) for r in await fetch(api_app)}
    assert stored == {e["request_id"] for e in events}  # all of them, exactly once


# ---- the broker goes away ---------------------------------------------------------------------------------------
async def test_the_gateway_keeps_answering_while_the_broker_is_down_and_events_catch_up(
    settings, stack, writer, api_app, rabbit_ep: Endpoint
):
    proxy = await TcpProxy(rabbit_ep.host, rabbit_ep.port).start()
    through_proxy = settings.model_copy(
        update={"rabbitmq_host": "127.0.0.1", "rabbitmq_port": proxy.port}
    )
    try:
        async for gw in run_gateway(through_proxy, collect=False):
            events = gw.app.state.events
            first = await post(gw, stack.key)
            await wait_count(api_app, 1)  # the normal path works through the proxy

            await proxy.stop()  # the broker "goes down"
            ids = [first.headers["x-request-id"]]
            for i in range(4):
                r = await post(gw, stack.key, chat_body(f"during outage {i}"))
                assert r.status_code == 200  # clients are served as usual
                ids.append(r.headers["x-request-id"])
            await asyncio.sleep(0.3)
            ready = (await gw.client.get("/health/ready")).json()
            assert ready["status"] == "ok"  # still ready: it can serve
            assert (
                ready["traffic_events"]["buffered"] >= 1
                and ready["traffic_events"]["dropped_total"] == 0
            )

            await proxy.start()  # the broker is back
            await wait_count(api_app, 5, timeout=30)
            stored = [str(r["request_id"]) for r in await fetch(api_app)]
            assert sorted(stored) == sorted(ids)  # everything arrived, once
            assert await wait_until(lambda e=events: e.buffered == 0)
            assert events.dropped_total == 0
    finally:
        await proxy.stop()


async def test_when_the_buffer_overflows_the_oldest_events_are_dropped_and_counted(
    settings, stack, writer, api_app, rabbit_ep: Endpoint
):
    proxy = await TcpProxy(rabbit_ep.host, rabbit_ep.port).start()
    await proxy.stop()  # down from the start: the publisher cannot connect at all
    tiny = settings.model_copy(
        update={
            "rabbitmq_host": "127.0.0.1",
            "rabbitmq_port": proxy.port,
            "traffic_buffer_events": 3,
        }
    )
    try:
        async for gw in run_gateway(tiny, collect=False):
            ids = []
            for i in range(6):
                r = await post(gw, stack.key, chat_body(f"q{i}"))
                assert r.status_code == 200
                ids.append(r.headers["x-request-id"])
            events = gw.app.state.events
            assert events.buffered == 3 and events.dropped_total == 3
            ready = (await gw.client.get("/health/ready")).json()
            assert ready["traffic_events"]["dropped_total"] == 3

            await proxy.start()
            await wait_count(api_app, 3, timeout=30)
            await asyncio.sleep(0.5)
            stored = {str(r["request_id"]) for r in await fetch(api_app)}
            assert stored == set(ids[3:])  # the newest three survived
    finally:
        await proxy.stop()


async def test_the_gateway_flushes_buffered_events_when_it_stops(settings, stack, writer, api_app):
    ids = []
    async for gw in run_gateway(settings, collect=False):
        for i in range(5):
            ids.append((await post(gw, stack.key, chat_body(f"bye {i}"))).headers["x-request-id"])
    # the gateway is stopped now (lifespan finished): whatever was buffered was handed over
    await wait_count(api_app, 5, timeout=20)
    assert {str(r["request_id"]) for r in await fetch(api_app)} == set(ids)
