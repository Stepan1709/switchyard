"""Shared limits of calibration workers, kept in Redis so that every worker process sees them.

ProviderLimiter  how hard workers may hit one provider: a cap on simultaneous calls (slots)
                 and a cap on calls per second (token bucket), both checked in one atomic
                 Lua script. A slot has an expiry time, so a worker that is killed while
                 holding one does not block the provider forever.
RunBudget        how much a run may still spend: reserved before a call, settled after it.
                 Counters are atomic (Lua). Redis is a cache of the truth that lives in the
                 database (run_results): when the counters are missing they are rebuilt from it.
"""

import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.messaging.errors import Deferred

# KEYS[1] slots (zset member -> expiry ms)   KEYS[2] token bucket (hash tokens, ts)
# ARGV  max_concurrency, rate per second, burst, slot ttl ms, member
# returns {granted, wait_ms}
_ACQUIRE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local max_conc = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local burst = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= max_conc then
  return {0, 100}
end
local b = redis.call('HMGET', KEYS[2], 'tokens', 'ts')
local tokens = tonumber(b[1])
local ts = tonumber(b[2])
if tokens == nil then
  tokens = burst
  ts = now
end
tokens = math.min(burst, tokens + (now - ts) * rate / 1000)
if tokens < 1 then
  redis.call('HSET', KEYS[2], 'tokens', tostring(tokens), 'ts', now)
  redis.call('PEXPIRE', KEYS[2], 60000)
  return {0, math.ceil((1 - tokens) * 1000 / rate)}
end
redis.call('HSET', KEYS[2], 'tokens', tostring(tokens - 1), 'ts', now)
redis.call('PEXPIRE', KEYS[2], 60000)
redis.call('ZADD', KEYS[1], now + ttl, ARGV[5])
redis.call('PEXPIRE', KEYS[1], ttl + 60000)
return {1, 0}
"""

# KEYS[1] money counter (spent + reserved, millionths of USD)   KEYS[2] call counter
# ARGV  estimate, max money (-1 = none), max calls (-1 = none), ttl seconds
# returns 1 reserved, 0 over budget, -1 counters are missing (must be rebuilt first)
_RESERVE = """
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('EXISTS', KEYS[2]) == 0 then
  return -1
end
local estimate = tonumber(ARGV[1])
local max_money = tonumber(ARGV[2])
local max_calls = tonumber(ARGV[3])
local money = tonumber(redis.call('GET', KEYS[1]))
local calls = tonumber(redis.call('GET', KEYS[2]))
if max_calls >= 0 and calls + 1 > max_calls then
  return 0
end
if max_money >= 0 and money + estimate > max_money then
  return 0
end
redis.call('INCRBY', KEYS[1], estimate)
redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[1], ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[4])
return 1
"""


@dataclass(frozen=True)
class Slot:
    provider_id: str
    member: str


class ProviderLimiter:
    def __init__(self, redis: Redis, *, slot_ttl: float, wait: float) -> None:
        self._redis = redis
        self._ttl_ms = int(slot_ttl * 1000)
        self._wait = wait
        self._script = redis.register_script(_ACQUIRE)

    @staticmethod
    def _keys(provider_id: str) -> list[str]:
        return [f"calib:slots:{provider_id}", f"calib:bucket:{provider_id}"]

    async def try_acquire(
        self, provider_id: str, *, max_concurrency: int, max_rps: float
    ) -> tuple[Slot | None, float]:
        """One attempt. Returns (slot, 0) or (None, seconds to wait before trying again)."""
        member = uuid.uuid4().hex
        burst = max(1.0, max_rps)
        granted, wait_ms = await self._script(
            keys=self._keys(provider_id),
            args=[max_concurrency, max_rps, burst, self._ttl_ms, member],
        )
        if int(granted):
            return Slot(provider_id, member), 0.0
        return None, int(wait_ms) / 1000

    async def release(self, slot: Slot) -> None:
        with contextlib.suppress(Exception):  # the slot expires by itself anyway
            await self._redis.zrem(self._keys(slot.provider_id)[0], slot.member)

    @contextlib.asynccontextmanager
    async def slot(
        self, provider_id: str, *, max_concurrency: int, max_rps: float
    ) -> AsyncIterator[Slot]:
        """Wait a little for a slot; if none frees up, raise Deferred (the job comes back later
        without using up a retry)."""
        import asyncio
        import time

        deadline = time.monotonic() + self._wait
        while True:
            slot, wait = await self.try_acquire(
                provider_id, max_concurrency=max_concurrency, max_rps=max_rps
            )
            if slot is not None:
                break
            if time.monotonic() + wait > deadline:
                raise Deferred(f"provider {provider_id} is at its limit")
            await asyncio.sleep(wait)
        try:
            yield slot
        finally:
            await self.release(slot)


class RunBudget:
    """Reserve-then-settle accounting of one run's spending and number of provider calls."""

    def __init__(self, redis: Redis, run_id: str, *, ttl_seconds: int = 7 * 24 * 3600) -> None:
        self._redis = redis
        self._money = f"calib:budget:{run_id}:micro"
        self._calls = f"calib:budget:{run_id}:calls"
        self._ttl = ttl_seconds
        self._reserve_script = redis.register_script(_RESERVE)

    async def reserve(
        self,
        estimate_micro: int,
        *,
        max_micro: int | None,
        max_calls: int | None,
        rebuild: Callable[[], Awaitable[tuple[int, int]]],
    ) -> bool:
        """True if the call fits the budget (and is now counted). `rebuild` returns
        (spent micro-USD, calls made) from the database when the counters are missing."""
        args = [estimate_micro, -1 if max_micro is None else max_micro]
        args += [-1 if max_calls is None else max_calls, self._ttl]
        for _ in range(2):
            outcome = int(await self._reserve_script(keys=[self._money, self._calls], args=args))
            if outcome >= 0:
                return outcome == 1
            spent, calls = await rebuild()
            # SET NX: if another worker rebuilt it first, its value stands (no double counting)
            await self._redis.set(self._money, spent, nx=True, ex=self._ttl)
            await self._redis.set(self._calls, calls, nx=True, ex=self._ttl)
        raise RuntimeError("budget counters could not be initialised")

    async def settle(self, estimate_micro: int, actual_micro: int) -> None:
        """Replace the reservation by what the call really cost."""
        if actual_micro != estimate_micro:
            await self._redis.incrby(self._money, actual_micro - estimate_micro)

    async def refund(self, estimate_micro: int, *, keep_call: bool) -> None:
        """The call produced no usable cost (it failed before being billed)."""
        await self._redis.incrby(self._money, -estimate_micro)
        if not keep_call:
            await self._redis.decr(self._calls)
