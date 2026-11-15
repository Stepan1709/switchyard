"""Queue consumer: manual ack, retries with backoff, dead-lettering, graceful stop.

Message life cycle (see also retry.py):
  handler returns            -> ack
  Deferred                   -> copy to Q.retry.1 with the retry counter unchanged, then ack
  TemporaryError             -> copy to Q.retry.<n> (delay grows with n), then ack the original;
                                after the last retry the copy goes to Q.dlq instead
  PermanentError / unknown   -> copy to Q.dlq with the reason in its headers, then ack
  broker unreachable while handling the failure -> nack with requeue: the message is never lost
  worker killed mid-handling -> the broker redelivers it to another consumer (it was not acked)

Delivery is therefore at-least-once. Handlers must be idempotent; use the inbox
(app/core/inbox.py) to drop duplicates.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)

from app.core.logging import correlation_id_var, get_logger
from app.core.messaging import retry
from app.core.messaging.envelope import H_RETRY, InvalidMessage, Message
from app.core.messaging.errors import PublishError
from app.core.messaging.publisher import Publisher
from app.core.messaging.topology import QueueNames, declare_work_queue

log = get_logger(__name__)

Handler = Callable[[Message], Awaitable[None]]


@dataclass
class ConsumerSpec:
    name: str  # work queue name, e.g. "worker.request_log"
    bindings: list[str]  # routing keys on the events exchange
    handler: Handler
    prefetch: int | None = None  # None = the default from settings
    # Optional hooks for a handler that owns background work (for example a batch flusher):
    # on_start runs before messages are taken, on_stop after the running handlers have finished.
    on_start: Callable[[], Awaitable[None]] | None = None
    on_stop: Callable[[], Awaitable[None]] | None = None


@dataclass
class Consumer:
    spec: ConsumerSpec
    connection: AbstractRobustConnection
    publisher: Publisher
    retry_delays_ms: list[int]
    delivery_limit: int = 10
    default_prefetch: int = 10
    requeue_pause: float = 1.0

    _channel: AbstractChannel | None = field(default=None, init=False)
    _queue: AbstractQueue | None = field(default=None, init=False)
    _tag: str | None = field(default=None, init=False)
    _inflight: int = field(default=0, init=False)
    _idle: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        self._idle.set()
        self._names = QueueNames(self.spec.name)

    @property
    def running(self) -> bool:
        return self._tag is not None

    async def start(self) -> None:
        # One channel per consumer: the prefetch limit applies per channel.
        self._channel = await self.connection.channel()
        await self._channel.set_qos(prefetch_count=self.spec.prefetch or self.default_prefetch)
        self._queue = await declare_work_queue(
            self._channel,
            self.spec.name,
            self.spec.bindings,
            retry_delays_ms=self.retry_delays_ms,
            delivery_limit=self.delivery_limit,
        )
        if self.spec.on_start is not None:
            await self.spec.on_start()
        self._tag = await self._queue.consume(self._on_message, no_ack=False)
        log.info("consumer_started", queue=self.spec.name)

    async def stop(self, wait: float) -> bool:
        """Stop taking messages, wait for the running handlers, close the channel.

        Returns False if some handlers did not finish in time. Their messages were not acked,
        so the broker gives them to another consumer.
        """
        if self._queue is not None and self._tag is not None:
            await self._queue.cancel(self._tag)  # no new deliveries
            self._tag = None
        finished = True
        try:
            await asyncio.wait_for(self._idle.wait(), wait)
        except TimeoutError:
            finished = False
            log.warning("consumer_stop_timeout", queue=self.spec.name, inflight=self._inflight)
        if self.spec.on_stop is not None:
            await self.spec.on_stop()
        if self._channel is not None:
            await self._channel.close()
        log.info("consumer_stopped", queue=self.spec.name, clean=finished)
        return finished

    # ---- message handling ----------------------------------------------------------------
    async def _on_message(self, incoming: AbstractIncomingMessage) -> None:
        self._inflight += 1
        self._idle.clear()
        try:
            await self._handle(incoming)
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    async def _handle(self, incoming: AbstractIncomingMessage) -> None:
        try:
            message = Message.from_incoming(incoming)
        except InvalidMessage as exc:
            await self._fail(incoming, self._raw_message(incoming), exc)
            return

        token = correlation_id_var.set(message.correlation_id or message.message_id)
        try:
            await self.spec.handler(message)
        except asyncio.CancelledError:
            raise  # shutting down: leave the message unacked so that it is redelivered
        except Exception as exc:
            await self._fail(incoming, message, exc)
        else:
            await self._ack(incoming)
            log.debug("message_done", queue=self.spec.name, message_id=message.message_id)
        finally:
            correlation_id_var.reset(token)

    async def _fail(
        self, incoming: AbstractIncomingMessage, message: Message, exc: Exception
    ) -> None:
        decision = retry.decide(exc, message.attempt, len(self.retry_delays_ms))
        try:
            if decision.action == "defer":
                # Back through the first retry queue with the SAME attempt number.
                await self.publisher.publish_dlx(
                    self._names.retry(1), message, extra_headers={H_RETRY: message.attempt}
                )
                log.debug("message_deferred", queue=self.spec.name, message_id=message.message_id)
            elif decision.action == "retry":
                n = decision.next_attempt
                await self.publisher.publish_dlx(
                    self._names.retry(n),
                    message,
                    extra_headers={H_RETRY: n, "x-last-error": _short(exc)},
                )
                log.warning(
                    "message_retry_scheduled",
                    queue=self.spec.name,
                    message_id=message.message_id,
                    attempt=n,
                    error=type(exc).__name__,
                )
            else:
                await self.publisher.publish_dlx(
                    self._names.dlq,
                    message,
                    extra_headers={
                        "x-error-reason": decision.reason,
                        "x-error-type": type(exc).__name__,
                        "x-error-message": _short(exc),
                        "x-failed-at": datetime.now(UTC).isoformat(),
                        "x-original-queue": self.spec.name,
                        H_RETRY: message.attempt,
                    },
                )
                log.error(
                    "message_dead_lettered",
                    queue=self.spec.name,
                    message_id=message.message_id,
                    reason=decision.reason,
                    error=type(exc).__name__,
                )
        except PublishError as publish_exc:
            # The broker would not take the retry/DLQ copy. Keep the original: put it back.
            log.error("failure_publish_failed", queue=self.spec.name, error=str(publish_exc))
            await asyncio.sleep(self.requeue_pause)  # avoid a hot loop while the broker is sick
            await self._safe(incoming.nack(requeue=True))
            return
        await self._ack(incoming)

    async def _ack(self, incoming: AbstractIncomingMessage) -> None:
        # If the channel died meanwhile the broker redelivers; idempotent handlers make that safe.
        await self._safe(incoming.ack())

    @staticmethod
    async def _safe(op: Awaitable[Any]) -> None:
        try:
            await op
        except Exception as exc:
            log.warning("ack_failed", error=type(exc).__name__)

    @staticmethod
    def _raw_message(incoming: AbstractIncomingMessage) -> Message:
        """Wrap an undecodable message so that it can still be dead-lettered and inspected."""
        import uuid

        valid_id = incoming.message_id or str(uuid.uuid4())
        try:
            uuid.UUID(valid_id)
        except ValueError:
            valid_id = str(uuid.uuid4())
        return Message(
            type=incoming.type or "",
            payload=incoming.body.decode(errors="replace")[:10_000],
            message_id=valid_id,
            correlation_id=incoming.correlation_id,
            headers=dict(incoming.headers or {}),
        )


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]
