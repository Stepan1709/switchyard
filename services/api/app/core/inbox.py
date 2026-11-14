"""Inbox: drop duplicate deliveries.

RabbitMQ delivers at least once, so the same message can arrive twice (a worker died after
doing the work but before the ack; the outbox relay re-sent a row). The consumer records
`message_id` in `processed_messages` in the SAME transaction as its business effect:

    claim -> inserted   : this is the first time, do the work, commit both together
    claim -> conflict   : already processed, skip
    work fails          : the transaction rolls back, the claim disappears, a retry can run

The key is (consumer, message_id): the same event may legitimately be handled once by each of
several different consumers.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, String, delete, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.messaging.envelope import Message


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    consumer: Mapped[str] = mapped_column(String(100), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


async def claim(session: AsyncSession, consumer: str, message_id: str) -> bool:
    """Record the message as processed. True if this is the first time, False for a duplicate."""
    stmt = (
        pg_insert(ProcessedMessage)
        .values(consumer=consumer, message_id=uuid.UUID(message_id))
        .on_conflict_do_nothing()
        .returning(ProcessedMessage.message_id)
    )
    return (await session.execute(stmt)).first() is not None


async def process_once(
    session_factory: async_sessionmaker[AsyncSession],
    consumer: str,
    message: Message,
    work: Callable[[AsyncSession], Awaitable[None]],
) -> bool:
    """Run `work` exactly once per (consumer, message_id). Returns False for a duplicate."""
    async with session_factory() as session, session.begin():
        if not await claim(session, consumer, message.message_id):
            return False
        await work(session)  # raises -> the whole transaction, including the claim, rolls back
    return True


async def purge_processed(
    session_factory: async_sessionmaker[AsyncSession], retention_days: int
) -> int:
    """Forget old records. A duplicate arriving after the retention period would be processed
    again, so keep this well above the longest time a message can still be redelivered."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    async with session_factory() as session, session.begin():
        result = await session.execute(
            delete(ProcessedMessage).where(ProcessedMessage.processed_at < cutoff)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
