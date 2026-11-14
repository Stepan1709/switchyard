"""`python -m app.cli dlq ...`: look at dead letters and put them back."""

import asyncio
import json

import pytest

from app.cli import main
from app.core.messaging import Consumer, ConsumerSpec, Message, PermanentError
from app.core.messaging.topology import QueueNames
from tests.integration.conftest import PASSWORD, Endpoint, queue_depth, wait_until

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def broker_env(monkeypatch: pytest.MonkeyPatch, rabbit_ep: Endpoint):
    monkeypatch.setenv("RABBITMQ_HOST", rabbit_ep.host)
    monkeypatch.setenv("RABBITMQ_PORT", str(rabbit_ep.port))
    monkeypatch.setenv("RABBITMQ_PASSWORD", PASSWORD)


async def run_cli(*args: str) -> int:
    # the CLI runs its own event loop, so it is called from a worker thread
    return await asyncio.to_thread(main, list(args))


async def make_dead_letters(amqp, publisher, queue_name, n=2):
    async def failing(message):
        raise PermanentError(f"cannot handle {message.payload['n']}")

    consumer = Consumer(
        ConsumerSpec(queue_name, [f"{queue_name}.event"], failing),
        amqp,
        publisher,
        retry_delays_ms=[100],
    )
    await consumer.start()
    ids = []
    for i in range(n):
        message = Message(type=f"{queue_name}.event", payload={"n": i, "secret": "user-data"})
        await publisher.publish_event(message)
        ids.append(message.message_id)
    await wait_until(lambda: queue_depth(amqp, QueueNames(queue_name).dlq))
    await asyncio.sleep(0.3)
    await consumer.stop(5)
    return ids


async def test_list_shows_the_reason_but_not_the_payload_and_changes_nothing(
    amqp, publisher, queue_name, capsys
):
    ids = await make_dead_letters(amqp, publisher, queue_name)
    capsys.readouterr()

    assert await run_cli("dlq", "list", "--queue", queue_name) == 0
    out = capsys.readouterr().out
    items = [json.loads(line) for line in out.splitlines()]
    assert {i["message_id"] for i in items} == set(ids)
    assert {i["reason"] for i in items} == {"permanent"}
    assert all("cannot handle" in i["error"] for i in items)
    assert "user-data" not in out  # bodies can hold user data: hidden unless asked for
    assert await queue_depth(amqp, QueueNames(queue_name).dlq) == 2  # listing does not consume

    assert (
        await run_cli("dlq", "list", "--queue", queue_name, "--show-payload", "--limit", "1") == 0
    )
    shown = json.loads(capsys.readouterr().out.splitlines()[0])
    assert "user-data" in shown["payload"]


async def test_list_of_an_empty_dlq(amqp, publisher, queue_name, capsys):
    await make_dead_letters(amqp, publisher, queue_name, n=1)
    await run_cli("dlq", "requeue", "--queue", queue_name)
    capsys.readouterr()
    await run_cli("dlq", "list", "--queue", queue_name)
    assert "empty" in capsys.readouterr().out


async def test_requeue_returns_messages_to_the_work_queue_with_fresh_attempts(
    amqp, publisher, queue_name, capsys
):
    ids = await make_dead_letters(amqp, publisher, queue_name)

    assert await run_cli("dlq", "requeue", "--queue", queue_name) == 0
    assert "requeued 2" in capsys.readouterr().out
    assert await queue_depth(amqp, QueueNames(queue_name).dlq) == 0

    seen = []

    async def good(message):
        seen.append(message)

    consumer = Consumer(
        ConsumerSpec(queue_name, [f"{queue_name}.event"], good),
        amqp,
        publisher,
        retry_delays_ms=[100],
    )
    await consumer.start()
    await wait_until(lambda: len(seen) == 2)
    await consumer.stop(5)

    assert {m.message_id for m in seen} == set(ids)  # the same messages, ids unchanged
    assert all(m.attempt == 0 for m in seen)  # the retry counter was reset
    assert all("x-requeued-at" in m.headers for m in seen)
    assert {m.payload["secret"] for m in seen} == {"user-data"}  # the body is byte-for-byte


async def test_requeue_respects_the_limit(amqp, publisher, queue_name, capsys):
    await make_dead_letters(amqp, publisher, queue_name, n=3)
    await run_cli("dlq", "requeue", "--queue", queue_name, "--limit", "1")
    assert "requeued 1" in capsys.readouterr().out
    assert await queue_depth(amqp, QueueNames(queue_name).dlq) == 2


async def test_unknown_queue_is_a_clean_error(capsys):
    assert await run_cli("dlq", "list", "--queue", "no_such_queue_anywhere") == 1
    assert "no dead-letter queue" in capsys.readouterr().err
