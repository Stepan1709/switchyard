"""RabbitMQ topology, declared idempotently by every process at start.

Exchanges
  switchyard.events  (topic, durable)   domain events and commands
  switchyard.dlx     (direct, durable)  retries, returns to the work queue, dead letters

For a work queue `Q` bound to routing keys K:
  Q               quorum, durable. Bound to events with K and to the DLX with key "Q" (the
                  way back from the retry queues). Dead-letters to "Q.dlq" when the broker's
                  delivery limit is exceeded (a consumer that keeps crashing on a message).
  Q.retry.1..N    quorum, durable. A queue-wide TTL = the delay of that attempt; expired
                  messages are dead-lettered to the DLX with key "Q" and land in Q again.
                  Separate queues per delay (instead of one queue with per-message TTL) so a
                  long delay never blocks messages with a short one behind it.
  Q.dlq           quorum, durable. The dead-letter queue; messages keep the failure reason in
                  their headers.

Changing a queue's arguments (TTL, limits) of an existing queue is refused by the broker
(PRECONDITION_FAILED): such a change needs a new queue name or a policy.
"""

from dataclasses import dataclass
from typing import Any

from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue

EVENTS_EXCHANGE = "switchyard.events"
DLX_EXCHANGE = "switchyard.dlx"


@dataclass(frozen=True)
class QueueNames:
    work: str

    @property
    def dlq(self) -> str:
        return f"{self.work}.dlq"

    def retry(self, attempt: int) -> str:
        return f"{self.work}.retry.{attempt}"


@dataclass(frozen=True)
class Exchanges:
    events: AbstractExchange
    dlx: AbstractExchange


async def declare_exchanges(channel: AbstractChannel) -> Exchanges:
    events = await channel.declare_exchange(EVENTS_EXCHANGE, ExchangeType.TOPIC, durable=True)
    dlx = await channel.declare_exchange(DLX_EXCHANGE, ExchangeType.DIRECT, durable=True)
    return Exchanges(events, dlx)


async def declare_work_queue(
    channel: AbstractChannel,
    name: str,
    bindings: list[str],
    *,
    retry_delays_ms: list[int],
    delivery_limit: int = 10,
) -> AbstractQueue:
    """Declare the work queue, its retry queues and DLQ, and bind them. Safe to call repeatedly."""
    exchanges = await declare_exchanges(channel)
    names = QueueNames(name)
    quorum: dict[str, Any] = {"x-queue-type": "quorum"}

    dlq = await channel.declare_queue(names.dlq, durable=True, arguments=quorum)
    await dlq.bind(exchanges.dlx, routing_key=names.dlq)

    for attempt, delay in enumerate(retry_delays_ms, start=1):
        retry = await channel.declare_queue(
            names.retry(attempt),
            durable=True,
            arguments={
                **quorum,
                "x-message-ttl": delay,
                "x-dead-letter-exchange": DLX_EXCHANGE,
                "x-dead-letter-routing-key": name,
            },
        )
        await retry.bind(exchanges.dlx, routing_key=names.retry(attempt))

    work = await channel.declare_queue(
        name,
        durable=True,
        arguments={
            **quorum,
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-dead-letter-routing-key": names.dlq,
            "x-delivery-limit": delivery_limit,
        },
    )
    await work.bind(exchanges.dlx, routing_key=name)  # the way back from the retry queues
    for key in bindings:
        await work.bind(exchanges.events, routing_key=key)
    return work


async def declare_broadcast_queue(channel: AbstractChannel, bindings: list[str]) -> AbstractQueue:
    """A private queue for one instance (for example one gateway receiving `rules.changed`).

    Exclusive and auto-deleted: it exists only while its owner is connected, so every running
    instance receives its own copy of each event (fan-out). Missed events are covered by the
    owner's periodic refresh, not by the queue.
    """
    exchanges = await declare_exchanges(channel)
    queue = await channel.declare_queue(exclusive=True, auto_delete=True)
    for key in bindings:
        await queue.bind(exchanges.events, routing_key=key)
    return queue
