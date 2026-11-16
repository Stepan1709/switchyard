"""The monthly budget of an API key, and what a request costs.

How a budget works
  Each key may have `monthly_budget_usd`. The gateway keeps the month's spending of such a key
  in one Redis counter (integer millionths of a dollar, so INCRBY is exact). Before a request is
  forwarded the counter is read: at or above the limit the request is refused (429,
  `budget_exceeded`). After the answer its cost is added. Cost is known only after the answer,
  so requests that are already running when the limit is crossed finish and are charged: the
  overshoot is at most what the requests "in flight" cost, never unbounded.

Redis is not the source of truth: the worker compares the counter with the sums in the traffic
log and raises it when it is too low (after a Redis restart, say). Both steps tolerate Redis
being down: a budget is protection, not part of answering correctly, so the gateway lets the
request through rather than failing it.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.snapshot import KeyInfo, RouteTarget
from app.modules.calibration.pricing import Price, cost_usd, to_micro

log = get_logger(__name__)

REDIS_TIMEOUT = 0.25  # seconds; the hot path never waits longer for the budget
COUNTER_TTL = 40 * 24 * 3600  # a month's counter outlives the month by a few days


def month_key(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def counter_name(key_id: str, month: str) -> str:
    return f"budget:{key_id}:{month}"


@lru_cache(maxsize=512)
def _price(cost_type: str, input_per_1m: str | None, output_per_1m: str | None) -> Price:
    if cost_type == "per_token" and input_per_1m is not None and output_per_1m is not None:
        return Price("per_token", Decimal(input_per_1m), Decimal(output_per_1m))
    return Price("fixed_monthly")  # also a model with no price yet: it adds nothing per request


def price_of(target: RouteTarget) -> Price:
    return _price(target.cost_type, target.price_input_per_1m, target.price_output_per_1m)


def request_cost(
    price: Price, prompt_tokens: int | None, completion_tokens: int | None
) -> Decimal | None:
    """What a finished request cost; None when the provider reported no tokens."""
    if prompt_tokens is None or completion_tokens is None:
        return None
    return cost_usd(price, prompt_tokens, completion_tokens)


class BudgetGuard:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def exceeded(self, key: KeyInfo) -> bool:
        """True when the key has used up this month's budget."""
        if key.monthly_budget_usd is None:
            return False
        limit = to_micro(Decimal(key.monthly_budget_usd))
        try:
            async with asyncio.timeout(REDIS_TIMEOUT):
                raw = await self._redis.get(counter_name(key.key_id, month_key()))
        except Exception as exc:  # fail open
            log.warning("budget_check_unavailable", error=type(exc).__name__)
            return False
        return raw is not None and int(raw) >= limit

    async def charge(self, key_id: str, cost: Decimal) -> None:
        """Add the cost of a finished request to the key's month."""
        micro = to_micro(cost)
        if micro <= 0:
            return
        name = counter_name(key_id, month_key())
        try:
            async with asyncio.timeout(REDIS_TIMEOUT * 4):
                await self._redis.incrby(name, micro)
                await self._redis.expire(name, COUNTER_TTL)
        except Exception as exc:  # the worker's reconciliation repairs a missed charge
            log.warning("budget_charge_failed", error=type(exc).__name__)
