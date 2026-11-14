"""Transactional outbox and its relay (real PostgreSQL and RabbitMQ)."""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.core.inbox import process_once
from app.core.messaging import Consumer, ConsumerSpec, PublishError, UnroutableError
from app.core.outbox import OutboxRelay, enqueue
from tests.integration.conftest import wait_until

pytestmark = pytest.mark.integration


class SpyPublisher:
    """Records what the relay publishes; can fail on demand."""

    def __init__(self, real=None):
        self.real = real
        self.sent: list[str] = []
        self.fail_with: Exception | None = None
        self.fail_after: int | None = None  # raise after this many successful publishes

    async def publish_event(self, message):
        if self.fail_with is not None:
            raise self.fail_with
        if self.real is not None:
            await self.real.publish_event(message)
        self.sent.append(message.message_id)
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            self.fail_with = None
            self.fail_after = None
            raise RuntimeError("the process dies right after publishing")


@pytest.fixture
def factory(api_app):
    return api_app.state.resources.session_factory


async def add_events(factory, n, event_type="outbox.test", start=0):
    ids = []
    async with factory() as s, s.begin():
        for i in range(n):
            ids.append(enqueue(s, event_type, {"i": start + i}, correlation_id="corr-1"))
    return ids


async def unsent(factory) -> int:
    async with factory() as s:
        return (
            await s.execute(text("SELECT count(*) FROM outbox WHERE sent_at IS NULL"))
        ).scalar_one()


async def test_event_is_published_only_if_the_transaction_commits(factory):
    async with factory() as s:
        enqueue(s, "outbox.test", {"never": "sent"})
        await s.rollback()  # the business transaction fails: no event may exist
    async with factory() as s:
        assert (await s.execute(text("SELECT count(*) FROM outbox"))).scalar_one() == 0


async def test_relay_publishes_committed_events_to_a_consumer(factory, amqp, publisher, queue_name):
    received = []

    async def handler(message):
        received.append(message)

    spec = ConsumerSpec(queue_name, ["outbox.test"], handler)
    consumer = Consumer(spec, amqp, publisher, retry_delays_ms=[100])
    await consumer.start()

    ids = await add_events(factory, 3)
    relay = OutboxRelay(factory, publisher, batch_size=10)
    assert await relay.relay_batch() == 3
    await wait_until(lambda: len(received) == 3)
    await consumer.stop(5)

    # the outbox row id is the message_id; the correlation id travels with it
    assert {m.message_id for m in received} == {str(i) for i in ids}
    assert {m.correlation_id for m in received} == {"corr-1"}
    assert await unsent(factory) == 0
    assert await relay.relay_batch() == 0  # nothing is sent twice


async def test_broker_outage_keeps_events_and_they_go_out_in_order_afterwards(factory):
    spy = SpyPublisher()
    ids = await add_events(factory, 4)
    relay = OutboxRelay(factory, spy, batch_size=10)

    spy.fail_with = PublishError("broker is down")
    assert await relay.relay_batch() == 0
    assert await unsent(factory) == 4  # nothing was lost
    async with factory() as s:
        row = (
            await s.execute(text("SELECT attempts, last_error FROM outbox ORDER BY seq LIMIT 1"))
        ).one()
    assert row.attempts == 1 and "broker is down" in row.last_error

    spy.fail_with = None  # the broker is back
    assert await relay.relay_batch() == 4
    assert spy.sent == [str(i) for i in ids]  # oldest first
    assert await unsent(factory) == 0


async def test_a_restarted_relay_continues_where_the_previous_one_stopped(factory):
    ids = await add_events(factory, 5)
    spy = SpyPublisher()
    first = OutboxRelay(factory, spy, batch_size=2)
    assert await first.relay_batch() == 2  # the first process sends one batch and "dies"
    assert await unsent(factory) == 3

    second = OutboxRelay(factory, spy, batch_size=2)  # a new process starts
    while await second.relay_batch():
        pass
    assert sorted(spy.sent) == sorted(str(i) for i in ids)
    assert len(spy.sent) == len(set(spy.sent)) == 5  # no duplicates, nothing missing


async def test_parallel_relays_do_not_publish_the_same_event_twice(factory):
    ids = await add_events(factory, 60)
    spy = SpyPublisher()

    async def worker():
        relay = OutboxRelay(factory, spy, batch_size=7)
        while await relay.relay_batch():
            pass

    await asyncio.gather(*(worker() for _ in range(4)))
    assert len(spy.sent) == 60 and set(spy.sent) == {str(i) for i in ids}  # FOR UPDATE SKIP LOCKED


async def test_crash_after_publishing_resends_but_the_inbox_drops_the_duplicate(
    factory, amqp, publisher, queue_name
):
    """At-least-once: a crash between 'published' and 'marked as sent' repeats the event."""
    effects = []

    async def handler(message):
        async def work(session):
            effects.append(message.message_id)

        await process_once(factory, "dup-test", message, work)

    consumer = Consumer(
        ConsumerSpec(queue_name, ["outbox.test"], handler), amqp, publisher, retry_delays_ms=[100]
    )
    await consumer.start()
    ids = await add_events(factory, 1)

    spy = SpyPublisher(real=publisher)
    spy.fail_after = 1  # publish succeeds, then the "process" dies before committing
    relay = OutboxRelay(factory, spy, batch_size=10)
    with pytest.raises(RuntimeError):
        await relay.relay_batch()
    assert await unsent(factory) == 1  # the row was not marked: the relay will send it again

    assert await relay.relay_batch() == 1  # the duplicate, with the same message_id
    await wait_until(lambda: len(effects) == 1)
    await asyncio.sleep(0.5)  # give the duplicate time to arrive and be dropped
    await consumer.stop(5)

    assert spy.sent == [str(ids[0]), str(ids[0])]  # sent twice ...
    assert effects == [str(ids[0])]  # ... handled once


async def test_events_nobody_subscribes_to_do_not_block_the_others(
    factory, amqp, publisher, queue_name
):
    received = []

    async def handler(message):
        received.append(message.payload["i"])

    consumer = Consumer(
        ConsumerSpec(queue_name, ["outbox.wanted"], handler), amqp, publisher, retry_delays_ms=[100]
    )
    await consumer.start()
    await add_events(factory, 1, event_type="outbox.nobody.listens", start=100)
    await add_events(factory, 2, event_type="outbox.wanted")

    relay = OutboxRelay(factory, publisher, batch_size=10, unroutable_attempts=3)
    assert await relay.relay_batch() == 2  # the unroutable one is skipped, the rest go out
    await wait_until(lambda: sorted(received) == [0, 1])
    assert await unsent(factory) == 1

    await relay.relay_batch()  # attempts 2
    assert await unsent(factory) == 1
    await relay.relay_batch()  # attempts 3: given up, recorded
    await consumer.stop(5)
    assert await unsent(factory) == 0
    async with factory() as s:
        error = (
            await s.execute(
                text("SELECT last_error FROM outbox WHERE type = 'outbox.nobody.listens'")
            )
        ).scalar_one()
    assert error.startswith("dropped, nobody subscribed")


async def test_unroutable_publish_is_reported_as_unroutable(publisher):
    from app.core.messaging import Message

    with pytest.raises(UnroutableError):
        await publisher.publish_event(Message(type=f"nobody.{uuid.uuid4().hex}", payload={}))


async def test_run_loop_picks_up_new_events_and_stops_cleanly(factory):
    spy = SpyPublisher()
    relay = OutboxRelay(factory, spy, poll_interval=0.05, batch_size=10)
    task = asyncio.create_task(relay.run())
    await asyncio.sleep(0.15)
    await add_events(factory, 2)
    await wait_until(lambda: len(spy.sent) == 2)
    relay.stop()
    await asyncio.wait_for(task, 2)
    assert relay.last_tick > 0 and relay.published_total == 2


async def test_run_loop_survives_a_database_error(factory, monkeypatch):
    spy = SpyPublisher()
    relay = OutboxRelay(factory, spy, poll_interval=0.05)
    real = relay.relay_batch
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database restarted")
        return await real()

    monkeypatch.setattr(relay, "relay_batch", flaky)
    task = asyncio.create_task(relay.run())
    await asyncio.sleep(0.05)
    await add_events(factory, 1)
    await wait_until(lambda: len(spy.sent) == 1)  # the loop kept going after the error
    relay.stop()
    await asyncio.wait_for(task, 2)
