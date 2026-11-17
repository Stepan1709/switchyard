"""Transactional outbox: events are written in the same database transaction as the data they
describe, and a relay publishes them to RabbitMQ afterwards.

Why: "update the database, then publish" can lose the event when the process dies between the
two steps; "publish, then update" can announce something that never happened. Writing the
event as a row in the same transaction makes both happen or neither. The relay then delivers
rows at least once (it can crash after publishing and before marking the row), so consumers
must drop duplicates by message_id (the inbox).
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.core import tracing
from app.core.db import Base
from app.core.logging import get_logger
from app.core.messaging.envelope import Message
from app.core.messaging.errors import PublishError, UnroutableError

log = get_logger(__name__)


class EventPublisher(Protocol):
    """What the relay needs from a publisher (the real one, or a test double)."""

    async def publish_event(self, message: Message) -> None: ...


class OutboxEvent(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        # The relay scans only unsent rows in insertion order; the index holds only those rows.
        Index("ix_outbox_pending", "seq", postgresql_where=text("sent_at is null")),
    )

    # The row id is the message_id: it is created once and stays the same on every re-send.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Insertion order. created_at is not enough: rows of one transaction share the same
    # timestamp (now() is the transaction start), so their order would be undefined.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    type: Mapped[str] = mapped_column(String(100))  # routing key
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500))
    # W3C traceparent of the request that wrote the event: the trace continues in the worker
    # that handles it, however long the row waits in the table.
    traceparent: Mapped[str | None] = mapped_column(String(64))


def enqueue(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
) -> uuid.UUID:
    """Add an event to the caller's transaction; it is published only if that commits."""
    from app.core.logging import correlation_id_var

    event = OutboxEvent(
        id=uuid.uuid4(),
        type=event_type,
        payload=payload,
        correlation_id=correlation_id or correlation_id_var.get(),
        traceparent=tracing.current_traceparent(),
    )
    session.add(event)
    return event.id


class OutboxRelay:
    """Publishes unsent outbox rows. Several relays can run at once: `FOR UPDATE SKIP LOCKED`
    makes each one take different rows, so a row is normally published by one relay only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        *,
        poll_interval: float = 0.5,
        batch_size: int = 100,
        unroutable_attempts: int = 5,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._unroutable_attempts = unroutable_attempts
        self._stop = asyncio.Event()
        self.last_tick: float = 0.0
        self.published_total = 0

    async def relay_batch(self) -> int:
        """Publish one batch and return how many rows were sent."""
        sent = 0
        async with self._session_factory() as session, session.begin():
            stmt = (
                select(OutboxEvent)
                .where(OutboxEvent.sent_at.is_(None))
                .order_by(OutboxEvent.seq)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = list((await session.execute(stmt)).scalars())
            for row in rows:
                message = Message(
                    type=row.type,
                    payload=row.payload,
                    message_id=str(row.id),
                    correlation_id=row.correlation_id,
                    headers={tracing.TRACEPARENT: row.traceparent} if row.traceparent else {},
                )
                try:
                    with tracing.producer_span(f"publish {row.type}", message.headers):
                        await self._publisher.publish_event(message)
                except UnroutableError as exc:
                    # Nobody is subscribed. That must not block the events behind this one:
                    # try a few times (a consumer may still be starting), then drop and log.
                    row.attempts += 1
                    row.last_error = str(exc)[:500]
                    if row.attempts >= self._unroutable_attempts:
                        row.sent_at = datetime.now(UTC)
                        row.last_error = f"dropped, nobody subscribed: {exc}"[:500]
                        log.error("outbox_event_dropped", message_id=str(row.id), type=row.type)
                    continue
                except PublishError as exc:
                    # Keep the row (and the rows after it, to keep the order) for the next try.
                    row.attempts += 1
                    row.last_error = str(exc)[:500]
                    log.warning(
                        "outbox_publish_failed", message_id=str(row.id), error=str(exc)[:200]
                    )
                    break
                row.sent_at = datetime.now(UTC)
                row.attempts += 1
                sent += 1
        # The transaction commits here. A crash before this point re-sends the rows on the
        # next run (duplicates are possible and expected).
        self.published_total += sent
        return sent

    async def run(self) -> None:
        """Poll until stop() is called. A failed batch is logged and tried again later."""
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                sent = await self.relay_batch()
            except Exception as exc:  # the relay must survive database or broker hiccups
                sent = 0
                log.error("outbox_relay_error", error=type(exc).__name__)
            self.last_tick = loop.time()
            if sent >= self._batch_size:
                continue  # a full batch: there is probably more, do not sleep
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self._poll_interval)

    def stop(self) -> None:
        self._stop.set()
