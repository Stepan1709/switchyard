"""Work queues shared by more than one process.

The gateway declares the traffic-log queue too, so events wait in RabbitMQ even while the
worker is down (without a queue the broker would have nowhere to put them). Declaring is
idempotent as long as every process uses the same arguments, which is why they come from here.
"""

from aio_pika.abc import AbstractChannel, AbstractQueue

from app.core.config import Settings
from app.core.events import FEEDBACK_SUBMITTED, REQUEST_COMPLETED
from app.core.messaging.topology import declare_work_queue

REQUEST_LOG_QUEUE = "worker.request_log"  # the default; the name comes from Settings


async def declare_request_log_queue(channel: AbstractChannel, settings: Settings) -> AbstractQueue:
    return await declare_work_queue(
        channel,
        settings.request_log_queue,
        [REQUEST_COMPLETED],
        retry_delays_ms=settings.retry_delays_ms,
        delivery_limit=settings.delivery_limit,
    )


async def declare_feedback_queue(channel: AbstractChannel, settings: Settings) -> AbstractQueue:
    """Ratings from clients. The gateway declares it for the same reason as the log queue: a
    rating must not find nowhere to go, and an unroutable event would block the ones behind it."""
    return await declare_work_queue(
        channel,
        settings.feedback_queue,
        [FEEDBACK_SUBMITTED],
        retry_delays_ms=settings.retry_delays_ms,
        delivery_limit=settings.delivery_limit,
    )
