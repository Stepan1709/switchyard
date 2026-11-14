"""Publisher and consumer on a real RabbitMQ: acks, retries, DLQ, redelivery, shutdown."""

import asyncio
import time
import uuid

import aio_pika
import pytest
from sqlalchemy import text

from app.core.inbox import process_once
from app.core.messaging import (
    Consumer,
    ConsumerSpec,
    Message,
    PermanentError,
    PublishError,
    TemporaryError,
)
from app.core.messaging.envelope import H_RETRY
from app.core.messaging.topology import QueueNames
from tests.integration.conftest import queue_depth, wait_until

pytestmark = pytest.mark.integration

DELAYS = [150, 300, 450]  # milliseconds, short so that the tests stay fast


def make_consumer(amqp, publisher, name, handler, *, delays=DELAYS, prefetch=10, bindings=None):
    spec = ConsumerSpec(name, bindings or [f"{name}.event"], handler, prefetch=prefetch)
    return Consumer(spec, amqp, publisher, retry_delays_ms=delays, requeue_pause=0.05)


async def send(publisher, name, payload=None, **kw):
    message = Message(type=f"{name}.event", payload=payload or {"n": 1}, **kw)
    await publisher.publish_event(message)
    return message


async def queue_is_empty(amqp, name):
    return await queue_depth(amqp, name) == 0


async def get_dead_letter(amqp, name):
    """The first message of the DLQ, read without removing it."""
    channel = await amqp.channel()
    queue = await channel.declare_queue(QueueNames(name).dlq, passive=True)
    incoming = await wait_until(lambda: queue.get(no_ack=False, fail=False))
    await incoming.nack(requeue=True)
    await channel.close()
    return incoming


async def test_success_is_acked_and_nothing_is_left_behind(amqp, publisher, queue_name):
    seen = []

    async def handler(message):
        seen.append(message)

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    await consumer.start()
    sent = await send(publisher, queue_name, {"hello": "world"}, correlation_id="corr-7")
    await wait_until(lambda: len(seen) == 1)
    await consumer.stop(5)

    assert seen[0].message_id == sent.message_id
    assert seen[0].payload == {"hello": "world"} and seen[0].correlation_id == "corr-7"
    assert seen[0].type == f"{queue_name}.event" and seen[0].attempt == 0
    assert await queue_depth(amqp, queue_name) == 0
    assert await queue_depth(amqp, QueueNames(queue_name).dlq) == 0


async def test_temporary_errors_are_retried_with_a_growing_delay(amqp, publisher, queue_name):
    calls = []

    async def handler(message):
        calls.append((time.monotonic(), message.attempt, message.message_id))
        if len(calls) < 3:
            raise TemporaryError("provider is busy")

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    await consumer.start()
    sent = await send(publisher, queue_name)
    await wait_until(lambda: len(calls) == 3)
    await asyncio.sleep(0.3)  # let the final ack happen
    await consumer.stop(5)

    assert [c[1] for c in calls] == [0, 1, 2]  # the attempt counter grows
    assert {c[2] for c in calls} == {sent.message_id}  # the message_id never changes
    assert calls[1][0] - calls[0][0] >= 0.14  # waited in retry.1 (150 ms)
    assert calls[2][0] - calls[1][0] >= 0.29  # waited in retry.2 (300 ms)
    assert await queue_depth(amqp, queue_name) == 0
    assert await queue_depth(amqp, QueueNames(queue_name).dlq) == 0


async def test_retries_run_out_and_the_message_lands_in_the_dlq_with_the_reason(
    amqp, publisher, queue_name
):
    calls = []

    async def handler(message):
        calls.append(message.attempt)
        raise TemporaryError("still failing")

    consumer = make_consumer(amqp, publisher, queue_name, handler, delays=[100, 100])
    await consumer.start()
    sent = await send(publisher, queue_name, {"data": 42})
    await wait_until(lambda: len(calls) == 3)  # the first try + 2 retries
    dead = await get_dead_letter(amqp, queue_name)
    await consumer.stop(5)

    assert calls == [0, 1, 2]
    assert dead.message_id == sent.message_id
    assert dead.headers["x-error-reason"] == "retries_exhausted"
    assert dead.headers["x-error-type"] == "TemporaryError"
    assert "still failing" in dead.headers["x-error-message"]
    assert dead.headers["x-original-queue"] == queue_name and dead.headers[H_RETRY] == 2
    assert "x-failed-at" in dead.headers
    assert b'"data":42' in dead.body  # the payload is kept intact for a later requeue
    assert await queue_depth(amqp, queue_name) == 0


async def test_permanent_error_goes_to_the_dlq_without_retries(amqp, publisher, queue_name):
    calls = []

    async def handler(message):
        calls.append(1)
        raise PermanentError("the payload makes no sense")

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    await consumer.start()
    await send(publisher, queue_name)
    dead = await get_dead_letter(amqp, queue_name)
    await asyncio.sleep(0.8)  # longer than every retry delay: nothing may come back
    await consumer.stop(5)

    assert calls == [1]
    assert dead.headers["x-error-reason"] == "permanent"
    assert dead.headers[H_RETRY] == 0
    for n in (1, 2, 3):
        assert await queue_depth(amqp, QueueNames(queue_name).retry(n)) == 0


async def test_unexpected_exception_is_treated_as_permanent(amqp, publisher, queue_name):
    async def handler(message):
        raise ZeroDivisionError("bug in the handler")

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    await consumer.start()
    await send(publisher, queue_name)
    dead = await get_dead_letter(amqp, queue_name)
    await consumer.stop(5)
    assert dead.headers["x-error-reason"] == "unexpected"
    assert dead.headers["x-error-type"] == "ZeroDivisionError"


async def test_poison_messages_are_dead_lettered_and_do_not_block_the_queue(
    amqp, publisher, queue_name
):
    seen = []

    async def handler(message):
        seen.append(message.payload)

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    await consumer.start()
    channel = await amqp.channel()
    exchange = await channel.get_exchange("switchyard.events")
    await exchange.publish(
        aio_pika.Message(body=b"{not json", message_id=str(uuid.uuid4())),
        routing_key=f"{queue_name}.event",
    )
    await exchange.publish(
        aio_pika.Message(body=b'{"ok": true}', message_id="not-a-uuid"),
        routing_key=f"{queue_name}.event",
    )  # the id is not a UUID
    await send(publisher, queue_name, {"fine": True})  # a good one behind them
    await wait_until(lambda: seen == [{"fine": True}])
    await wait_until(lambda: queue_depth(amqp, QueueNames(queue_name).dlq))
    await consumer.stop(5)

    assert await queue_depth(amqp, QueueNames(queue_name).dlq) == 2
    dead = await get_dead_letter(amqp, queue_name)
    assert dead.headers["x-error-reason"] == "invalid_message"


async def test_unroutable_message_is_reported_not_dropped(publisher):
    # mandatory publishing: no queue is bound to this key, so the broker returns the message
    with pytest.raises(PublishError):
        await publisher.publish_event(Message(type="nobody.listens.here", payload={}))


async def test_prefetch_limits_concurrent_handlers(amqp, publisher, queue_name):
    running, peak, done = 0, 0, 0
    release = asyncio.Event()

    async def handler(message):
        nonlocal running, peak, done
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        done += 1

    consumer = make_consumer(amqp, publisher, queue_name, handler, prefetch=3)
    await consumer.start()
    for i in range(8):
        await send(publisher, queue_name, {"i": i})
    await wait_until(lambda: running == 3)
    await asyncio.sleep(0.3)
    assert peak == 3  # the other five wait in the queue
    release.set()
    await wait_until(lambda: done == 8)
    await consumer.stop(5)


async def test_graceful_stop_lets_running_handlers_finish(amqp, publisher, queue_name):
    started, finished = asyncio.Event(), []

    async def handler(message):
        started.set()
        await asyncio.sleep(0.6)
        finished.append(message.message_id)

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    await consumer.start()
    sent = await send(publisher, queue_name)
    await started.wait()
    clean = await consumer.stop(5)  # called while the handler is running

    assert clean is True and finished == [sent.message_id]
    assert await queue_depth(amqp, queue_name) == 0  # it was acked, not redelivered


async def test_message_of_a_stuck_handler_goes_to_another_consumer(amqp, publisher, queue_name):
    """A consumer that cannot finish in time hands its unacked message back to the broker."""
    entered = asyncio.Event()

    async def stuck(message):
        entered.set()
        await asyncio.sleep(30)

    first = make_consumer(amqp, publisher, queue_name, stuck)
    await first.start()
    sent = await send(publisher, queue_name)
    await entered.wait()

    received = []

    async def healthy(message):
        received.append(message)

    second = make_consumer(amqp, publisher, queue_name, healthy)
    await second.start()
    assert await first.stop(0.2) is False  # timed out; the channel is closed anyway
    await wait_until(lambda: len(received) == 1)
    await second.stop(5)
    assert received[0].message_id == sent.message_id


async def test_failure_while_broker_refuses_the_retry_copy_does_not_lose_the_message(
    amqp, publisher, queue_name, monkeypatch
):
    """If the retry copy cannot be published, the original is requeued instead of dropped."""
    outcomes = []

    async def handler(message):
        outcomes.append(message.attempt)
        if len(outcomes) == 1:
            raise TemporaryError("first try fails")

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    real = consumer.publisher.publish_dlx
    failures = {"left": 1}

    async def flaky(*args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise PublishError("broker said no")
        return await real(*args, **kwargs)

    monkeypatch.setattr(consumer.publisher, "publish_dlx", flaky)
    await consumer.start()
    await send(publisher, queue_name)
    await wait_until(lambda: len(outcomes) >= 2)  # redelivered after the nack
    await consumer.stop(5)
    assert len(outcomes) >= 2


# ---- the inbox: duplicates are dropped ----
@pytest.fixture
async def inbox_db(api_app):
    async with api_app.state.resources.session_factory() as s:
        await s.execute(text("CREATE TABLE effects (message_id uuid, consumer text)"))
        await s.commit()
    return api_app.state.resources.session_factory


async def test_duplicate_delivery_is_processed_once(amqp, publisher, queue_name, inbox_db):
    async def handler(message):
        async def work(session):
            await session.execute(
                text("INSERT INTO effects VALUES (:m, 'a')"), {"m": uuid.UUID(message.message_id)}
            )

        await process_once(inbox_db, "consumer-a", message, work)

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    await consumer.start()
    message = Message(type=f"{queue_name}.event", payload={"x": 1})
    await publisher.publish_event(message)
    await publisher.publish_event(message)  # the very same message_id delivered again
    await publisher.publish_event(message)
    await wait_until(lambda: queue_is_empty(amqp, queue_name))
    await asyncio.sleep(0.4)
    await consumer.stop(5)

    async with inbox_db() as s:
        effects = (await s.execute(text("SELECT count(*) FROM effects"))).scalar_one()
        claims = (await s.execute(text("SELECT count(*) FROM processed_messages"))).scalar_one()
    assert effects == 1 and claims == 1


async def test_the_inbox_is_scoped_per_consumer(inbox_db):
    from app.core.inbox import claim

    mid = str(uuid.uuid4())
    async with inbox_db() as s, s.begin():
        assert await claim(s, "consumer-a", mid) is True
        assert await claim(s, "consumer-a", mid) is False
        assert await claim(s, "consumer-b", mid) is True  # another consumer may handle it once


async def test_failed_work_rolls_back_the_claim_so_the_retry_runs(
    amqp, publisher, queue_name, inbox_db
):
    attempts = []

    async def handler(message):
        async def work(session):
            await session.execute(
                text("INSERT INTO effects VALUES (:m, 'a')"), {"m": uuid.UUID(message.message_id)}
            )
            attempts.append(message.attempt)
            if len(attempts) == 1:
                raise TemporaryError("fails after writing")  # the write must be rolled back

        await process_once(inbox_db, "consumer-a", message, work)

    consumer = make_consumer(amqp, publisher, queue_name, handler)
    await consumer.start()
    await send(publisher, queue_name)
    await wait_until(lambda: len(attempts) == 2)
    await asyncio.sleep(0.3)
    await consumer.stop(5)

    async with inbox_db() as s:
        effects = (await s.execute(text("SELECT count(*) FROM effects"))).scalar_one()
        claims = (await s.execute(text("SELECT count(*) FROM processed_messages"))).scalar_one()
    assert attempts == [0, 1]
    assert effects == 1 and claims == 1  # the first attempt left nothing behind


# ---- fan-out and cleanup ----------------------------------------------------------------------
async def test_broadcast_queues_give_every_instance_its_own_copy(amqp, publisher):
    from app.core.messaging import declare_broadcast_queue

    key = f"broadcast.{uuid.uuid4().hex[:8]}"
    channels = [await amqp.channel() for _ in range(2)]
    queues = [await declare_broadcast_queue(ch, [key]) for ch in channels]
    assert queues[0].name != queues[1].name  # each instance has a private, server-named queue

    sent = Message(type=key, payload={"version": 7})
    await publisher.publish_event(sent)
    for queue in queues:
        incoming = await wait_until(lambda q=queue: q.get(no_ack=True, fail=False))
        assert incoming.message_id == sent.message_id  # both instances got it

    # exclusive + auto-delete: the queue belongs to its connection and disappears with it
    assert all(q.exclusive and q.auto_delete and not q.durable for q in queues)


async def test_old_inbox_records_are_purged(inbox_db):
    from app.core.inbox import claim, purge_processed

    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    async with inbox_db() as s, s.begin():
        await claim(s, "c", old)
        await claim(s, "c", new)
        await s.execute(
            text(
                "UPDATE processed_messages SET processed_at = now() - interval '30 days'"
                " WHERE message_id = :m"
            ),
            {"m": uuid.UUID(old)},
        )
    assert await purge_processed(inbox_db, retention_days=14) == 1
    async with inbox_db() as s, s.begin():
        assert await claim(s, "c", old) is True  # forgotten: it would be processed again
        assert await claim(s, "c", new) is False  # still remembered
