"""RabbitMQ messaging: envelope, topology, publisher with confirms, consumer with retries."""

from app.core.messaging.consumer import Consumer, ConsumerSpec, Handler
from app.core.messaging.envelope import InvalidMessage, Message
from app.core.messaging.errors import (
    Deferred,
    PermanentError,
    PublishError,
    TemporaryError,
    UnroutableError,
)
from app.core.messaging.publisher import Publisher
from app.core.messaging.topology import (
    DLX_EXCHANGE,
    EVENTS_EXCHANGE,
    QueueNames,
    declare_broadcast_queue,
    declare_work_queue,
)

__all__ = [
    "DLX_EXCHANGE",
    "EVENTS_EXCHANGE",
    "Consumer",
    "ConsumerSpec",
    "Deferred",
    "Handler",
    "InvalidMessage",
    "Message",
    "PermanentError",
    "PublishError",
    "Publisher",
    "QueueNames",
    "TemporaryError",
    "UnroutableError",
    "declare_broadcast_queue",
    "declare_work_queue",
]
