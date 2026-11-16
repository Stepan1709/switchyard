"""Keeping the budget counters in Redis honest.

The truth is the cost column of the traffic log. The gateway's counters are a fast copy that can
be lost (Redis restarted) or miss a charge (Redis was unreachable for a moment). The worker
raises a counter that is lower than the log says; it never lowers one on its own, because the
log is a few seconds behind the gateway and lowering would let requests through that have
already been paid for. `recount` is the explicit "set it to what the log says" for a person.
"""

from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.modules.calibration.pricing import to_micro
from app.modules.gateway.budget import COUNTER_TTL, counter_name, month_key

log = get_logger(__name__)

_RAISE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if tonumber(ARGV[1]) > current then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""

_SPENT = text(
    """
    SELECT r.key_id, coalesce(sum(r.cost_usd), 0) AS spent
    FROM requests r JOIN api_keys k ON k.id = r.key_id
    WHERE k.monthly_budget_usd IS NOT NULL AND k.revoked_at IS NULL
      AND r.started_at >= :start AND r.started_at < :end
      AND (CAST(:key_id AS uuid) IS NULL OR r.key_id = :key_id)
    GROUP BY r.key_id
    """
)


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return start, end


async def spent_this_month(
    session_factory: async_sessionmaker[AsyncSession], now: datetime, key_id: str | None = None
) -> dict[str, Decimal]:
    """What the log says each budgeted key spent in the month of `now`."""
    start, end = month_bounds(now)
    async with session_factory() as session:
        rows = await session.execute(_SPENT, {"start": start, "end": end, "key_id": key_id})
        return {str(r.key_id): Decimal(r.spent) for r in rows}


async def reconcile(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, now: datetime | None = None
) -> int:
    """Raise every counter that is below the log. Returns how many were raised."""
    now = now or datetime.now(UTC)
    script = redis.register_script(_RAISE)
    raised = 0
    for key_id, spent in (await spent_this_month(session_factory, now)).items():
        micro = to_micro(spent)
        if micro <= 0:
            continue
        changed = await script(
            keys=[counter_name(key_id, month_key(now))], args=[micro, COUNTER_TTL]
        )
        raised += int(changed)
    if raised:
        log.info("budget_counters_raised", count=raised)
    return raised


async def recount(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    key_id: str,
    now: datetime | None = None,
) -> int:
    """Set one counter to exactly what the log says (down as well as up). Returns micro-USD."""
    now = now or datetime.now(UTC)
    spent = (await spent_this_month(session_factory, now, key_id)).get(key_id, Decimal(0))
    micro = to_micro(spent)
    await redis.set(counter_name(key_id, month_key(now)), micro, ex=COUNTER_TTL)
    return micro
