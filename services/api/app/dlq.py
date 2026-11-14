"""Operator tools for dead-letter queues: list failed messages with the reason, put them back.

Used through `python -m app.cli dlq ...`. Message bodies are not printed unless asked: they can
contain user data.
"""

import json
from datetime import UTC, datetime
from typing import Any

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.core.config import Settings
from app.core.messaging.envelope import H_RETRY
from app.core.messaging.topology import DLX_EXCHANGE, QueueNames


def _describe(incoming: AbstractIncomingMessage, show_payload: bool) -> dict[str, Any]:
    headers = dict(incoming.headers or {})
    info: dict[str, Any] = {
        "message_id": incoming.message_id,
        "type": incoming.type,
        "correlation_id": incoming.correlation_id,
        "reason": headers.get("x-error-reason"),
        "error_type": headers.get("x-error-type"),
        "error": headers.get("x-error-message"),
        "failed_at": headers.get("x-failed-at"),
        "retries": headers.get(H_RETRY),
        "body_bytes": len(incoming.body),
    }
    if show_payload:
        info["payload"] = incoming.body.decode(errors="replace")
    return info


async def list_dead_letters(
    settings: Settings, queue: str, limit: int, show_payload: bool = False
) -> list[dict[str, Any]]:
    connection = await aio_pika.connect(settings.amqp_url)
    try:
        channel = await connection.channel()
        dlq = await channel.declare_queue(QueueNames(queue).dlq, passive=True)
        taken: list[AbstractIncomingMessage] = []
        for _ in range(limit):
            incoming = await dlq.get(no_ack=False, fail=False)
            if incoming is None:
                break
            taken.append(incoming)
        described = [_describe(m, show_payload) for m in taken]
        for incoming in taken:  # give everything back: listing must not change the queue
            await incoming.nack(requeue=True)
        return described
    finally:
        await connection.close()


async def requeue_dead_letters(settings: Settings, queue: str, limit: int) -> int:
    """Move up to `limit` messages from `<queue>.dlq` back to the work queue.

    The body is copied byte for byte and the retry counter is reset, so the message gets a fresh
    set of attempts. The copy is confirmed by the broker before the original is acked.
    """
    connection = await aio_pika.connect(settings.amqp_url)
    moved = 0
    try:
        channel = await connection.channel()  # publisher confirms are on by default
        dlx = await channel.declare_exchange(
            DLX_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
        )
        dlq = await channel.declare_queue(QueueNames(queue).dlq, passive=True)
        for _ in range(limit):
            incoming = await dlq.get(no_ack=False, fail=False)
            if incoming is None:
                break
            headers = {
                **(incoming.headers or {}),
                H_RETRY: 0,
                "x-requeued-at": datetime.now(UTC).isoformat(),
            }
            copy = aio_pika.Message(
                body=incoming.body,
                content_type=incoming.content_type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=incoming.message_id,
                correlation_id=incoming.correlation_id,
                type=incoming.type,
                headers=headers,
            )
            try:
                await dlx.publish(copy, routing_key=queue, mandatory=True)
            except Exception:
                await incoming.nack(requeue=True)  # keep it in the DLQ if the copy was not accepted
                raise
            await incoming.ack()
            moved += 1
    finally:
        await connection.close()
    return moved


def format_lines(items: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(i, ensure_ascii=False, default=str) for i in items)
