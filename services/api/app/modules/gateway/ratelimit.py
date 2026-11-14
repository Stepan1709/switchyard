"""Per-key request rate limit on Redis (fixed one-minute windows).

One INCR per request. The counter is created together with its expiry in one Lua call, so a
crash can never leave a counter that never expires. If Redis is unavailable the limiter lets
the request through (the limit is protection, not part of correctness): a Redis outage must
not take the gateway down with it.
"""

import asyncio
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.logging import get_logger

log = get_logger(__name__)

_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""
WINDOW_SECONDS = 60
REDIS_TIMEOUT = 0.25  # seconds; the hot path never waits longer for the limiter


@dataclass(frozen=True)
class RateResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int  # until the window ends


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._script = redis.register_script(_LUA)

    async def hit(self, key_id: str, limit: int, now: float | None = None) -> RateResult | None:
        """Count one request. None means "Redis did not answer, do not limit"."""
        now = time.time() if now is None else now
        window = int(now // WINDOW_SECONDS)
        reset = WINDOW_SECONDS - int(now % WINDOW_SECONDS)
        try:
            async with asyncio.timeout(REDIS_TIMEOUT):
                count = int(
                    await self._script(keys=[f"rl:{key_id}:{window}"], args=[WINDOW_SECONDS + 1])
                )
        except Exception as exc:  # fail open
            log.warning("rate_limiter_unavailable", error=type(exc).__name__)
            return None
        return RateResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(limit - count, 0),
            reset_seconds=reset,
        )
