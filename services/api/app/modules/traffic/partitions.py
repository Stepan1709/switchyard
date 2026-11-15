"""Monthly partitions of the `requests` table: create ahead, drop what is past retention.

A partition holds one calendar month (UTC) and is named `requests_YYYY_MM`. Dropping a
partition removes a whole month at once: no row-by-row delete, no dead tuples, no vacuum.

Retention of N months keeps the current month and the N months before it.
"""

import re
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger

log = get_logger(__name__)

_NAME_RE = re.compile(r"requests_(\d{4})_(\d{2})")


def month_start(value: date | datetime) -> date:
    return date(value.year, value.month, 1)


def add_months(month: date, delta: int) -> date:
    index = month.year * 12 + (month.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def partition_name(month: date) -> str:
    return f"requests_{month.year:04d}_{month.month:02d}"


def month_of_name(name: str) -> date | None:
    match = _NAME_RE.fullmatch(name)
    return date(int(match.group(1)), int(match.group(2)), 1) if match else None


def create_partition_sql(month: date) -> str:
    # Names and bounds come from integers only (never from user input), so they are safe to
    # put into the statement text: DDL cannot take bind parameters.
    nxt = add_months(month, 1)
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(month)} PARTITION OF requests "
        f"FOR VALUES FROM ('{month.isoformat()} 00:00:00+00') TO ('{nxt.isoformat()} 00:00:00+00')"
    )


async def ensure_partitions(session: AsyncSession, months: list[date]) -> None:
    for month in sorted(set(months)):
        try:
            async with session.begin_nested():
                await session.execute(text(create_partition_sql(month)))
        except DBAPIError as exc:
            # Two processes creating the same month at once: one of them loses the race.
            if "already exists" not in str(exc):
                raise


async def ensure_window(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    *,
    ahead: int = 2,
    behind: int = 1,
) -> list[str]:
    """Make sure partitions exist from `behind` months ago to `ahead` months in the future."""
    current = month_start(now or datetime.now(UTC))
    months = [add_months(current, d) for d in range(-behind, ahead + 1)]
    async with session_factory() as session, session.begin():
        await ensure_partitions(session, months)
    return [partition_name(m) for m in months]


async def list_partitions(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = 'requests' ORDER BY c.relname"
        )
    )
    return [r[0] for r in rows]


async def drop_expired(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    *,
    retention_months: int,
) -> list[str]:
    """Drop partitions older than the retention period. Returns the names removed."""
    cutoff = add_months(month_start(now or datetime.now(UTC)), -retention_months)
    dropped: list[str] = []
    async with session_factory() as session, session.begin():
        for name in await list_partitions(session):
            month = month_of_name(name)
            if month is not None and month < cutoff:
                await session.execute(text(f"DROP TABLE {name}"))  # name matched _NAME_RE above
                dropped.append(name)
    if dropped:
        log.info("traffic_partitions_dropped", partitions=dropped)
    return dropped
