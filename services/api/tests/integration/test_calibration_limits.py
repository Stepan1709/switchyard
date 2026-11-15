"""Shared limits of calibration workers on a real Redis: provider slots, the call-rate bucket
and the run budget."""

import asyncio
import time
import uuid

import pytest
from redis.asyncio import Redis

from app.core.messaging import Deferred
from app.modules.calibration.limits import ProviderLimiter, RunBudget
from tests.integration.conftest import PASSWORD, Endpoint

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis(redis_ep: Endpoint):
    client = Redis(host=redis_ep.host, port=redis_ep.port, password=PASSWORD, decode_responses=True)
    yield client
    await client.aclose()


def provider() -> str:
    return str(uuid.uuid4())  # every test uses its own keys


def limiter(redis, ttl=30.0, wait=0.3) -> ProviderLimiter:
    return ProviderLimiter(redis, slot_ttl=ttl, wait=wait)


# ---- slots ------------------------------------------------------------------------------------
async def test_a_provider_never_has_more_calls_than_its_concurrency_limit(redis):
    lim, pid = limiter(redis), provider()
    held = [(await lim.try_acquire(pid, max_concurrency=2, max_rps=1000))[0] for _ in range(2)]
    assert all(held)
    slot, wait = await lim.try_acquire(pid, max_concurrency=2, max_rps=1000)
    assert slot is None and wait > 0  # full

    assert held[0] is not None
    await lim.release(held[0])
    again, _ = await lim.try_acquire(pid, max_concurrency=2, max_rps=1000)
    assert again is not None  # a freed slot is reusable at once


async def test_parallel_requests_for_slots_never_exceed_the_limit(redis):
    lim, pid = limiter(redis), provider()
    attempts = await asyncio.gather(
        *(lim.try_acquire(pid, max_concurrency=3, max_rps=1000) for _ in range(20))
    )
    assert sum(1 for slot, _ in attempts if slot) == 3


async def test_the_slot_of_a_dead_worker_is_freed_after_its_ttl(redis):
    lim, pid = limiter(redis, ttl=0.3), provider()
    slot, _ = await lim.try_acquire(pid, max_concurrency=1, max_rps=1000)
    assert slot is not None  # a worker takes it and is then killed: it never releases
    blocked, _ = await lim.try_acquire(pid, max_concurrency=1, max_rps=1000)
    assert blocked is None
    await asyncio.sleep(0.45)
    freed, _ = await lim.try_acquire(pid, max_concurrency=1, max_rps=1000)
    assert freed is not None


async def test_the_rate_limit_spaces_calls_out(redis):
    lim, pid = limiter(redis), provider()
    granted = 0
    waits = []
    for _ in range(8):  # rate 4/s with a burst of 4
        slot, wait = await lim.try_acquire(pid, max_concurrency=100, max_rps=4)
        if slot:
            granted += 1
            await lim.release(slot)
        else:
            waits.append(wait)
    assert granted == 4  # the burst
    assert waits and all(0 < w <= 0.26 for w in waits)  # next token in at most 1/4 s
    await asyncio.sleep(0.3)
    slot, _ = await lim.try_acquire(pid, max_concurrency=100, max_rps=4)
    assert slot is not None  # a token has been refilled


async def test_the_context_manager_waits_a_little_then_defers(redis):
    lim, pid = limiter(redis, wait=0.3), provider()
    async with lim.slot(pid, max_concurrency=1, max_rps=1000):
        started = time.monotonic()
        with pytest.raises(Deferred):
            async with lim.slot(pid, max_concurrency=1, max_rps=1000):
                raise AssertionError("must not get a slot")
        assert 0.1 < time.monotonic() - started < 1.5
    async with lim.slot(pid, max_concurrency=1, max_rps=1000):  # released on exit
        pass


async def test_the_slot_is_released_when_the_call_fails(redis):
    lim, pid = limiter(redis), provider()
    with pytest.raises(RuntimeError):
        async with lim.slot(pid, max_concurrency=1, max_rps=1000):
            raise RuntimeError("provider exploded")
    slot, _ = await lim.try_acquire(pid, max_concurrency=1, max_rps=1000)
    assert slot is not None


# ---- budget -----------------------------------------------------------------------------------
async def reserve(budget: RunBudget, micro: int, *, max_micro=None, max_calls=None, db=(0, 0)):
    rebuilt = []

    async def rebuild():
        rebuilt.append(1)
        return db

    ok = await budget.reserve(micro, max_micro=max_micro, max_calls=max_calls, rebuild=rebuild)
    return ok, len(rebuilt)


async def test_money_is_reserved_until_the_limit_is_reached(redis):
    budget = RunBudget(redis, str(uuid.uuid4()))
    assert (await reserve(budget, 400, max_micro=1000))[0]
    assert (await reserve(budget, 400, max_micro=1000))[0]
    assert not (await reserve(budget, 400, max_micro=1000))[0]  # 1200 > 1000
    assert (await reserve(budget, 200, max_micro=1000))[0]  # exactly 1000 still fits


async def test_settling_returns_the_unused_part_of_a_reservation(redis):
    budget = RunBudget(redis, str(uuid.uuid4()))
    assert (await reserve(budget, 800, max_micro=1000))[0]
    assert not (await reserve(budget, 800, max_micro=1000))[0]
    await budget.settle(800, 100)  # the call turned out to cost 100
    assert (await reserve(budget, 800, max_micro=1000))[0]


async def test_a_failed_call_gives_its_money_back(redis):
    budget = RunBudget(redis, str(uuid.uuid4()))
    assert (await reserve(budget, 900, max_micro=1000, max_calls=2))[0]
    await budget.refund(900, keep_call=False)
    assert (await reserve(budget, 900, max_micro=1000, max_calls=2))[0]


async def test_the_number_of_calls_is_limited_too(redis):
    budget = RunBudget(redis, str(uuid.uuid4()))
    assert (await reserve(budget, 0, max_calls=2))[0]
    assert (await reserve(budget, 0, max_calls=2))[0]
    assert not (await reserve(budget, 0, max_calls=2))[0]


async def test_parallel_reservations_never_overspend(redis):
    budget = RunBudget(redis, str(uuid.uuid4()))
    outcomes = await asyncio.gather(
        *(reserve(budget, 100, max_micro=1000, max_calls=7) for _ in range(40))
    )
    assert sum(1 for ok, _ in outcomes if ok) == 7  # the call limit is the tighter one


async def test_lost_counters_are_rebuilt_once_from_the_database(redis):
    budget = RunBudget(redis, str(uuid.uuid4()))
    assert await reserve(budget, 100, max_micro=1000, db=(850, 5)) == (True, 1)  # 850 + 100 fits
    assert await reserve(budget, 100, max_micro=1000, db=(0, 0)) == (False, 0)  # 950 + 100 does not
    # (and the counters were not rebuilt a second time: the second `db` was never consulted)


async def test_workers_that_notice_the_loss_together_do_not_double_count(redis):
    run = str(uuid.uuid4())
    budget = RunBudget(redis, run)
    outcomes = await asyncio.gather(
        *(reserve(budget, 100, max_micro=10_000, db=(500, 1)) for _ in range(3))
    )
    assert all(ok for ok, _ in outcomes)
    # the stored 500 was taken over once (SET NX), then three reservations of 100 were added
    assert await redis.get(f"calib:budget:{run}:micro") == "800"
    assert await redis.get(f"calib:budget:{run}:calls") == "4"
