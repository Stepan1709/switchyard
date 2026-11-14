import asyncio

import pytest
from redis.asyncio import Redis

from app.modules.auth.rate_limit import LoginLimiter
from tests.integration.conftest import PASSWORD, Endpoint

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis(redis_ep: Endpoint):
    r = Redis(host=redis_ep.host, port=redis_ep.port, password=PASSWORD, decode_responses=True)
    await r.flushall()
    yield r
    await r.aclose()


def limiter(redis, window=60):
    return LoginLimiter(redis, max_per_email=3, max_per_ip=5, window_seconds=window)


async def test_blocks_after_limit_and_reports_wait(redis):
    lim = limiter(redis)
    for _ in range(3):
        assert await lim.retry_after("a@x.io", "1.1.1.1") == 0
        await lim.register_failure("a@x.io", "1.1.1.1")
    wait = await lim.retry_after("a@x.io", "1.1.1.1")
    assert 0 < wait <= 60
    assert await lim.retry_after("b@x.io", "2.2.2.2") == 0  # other email and IP unaffected


async def test_ip_limit_applies_across_emails(redis):
    lim = limiter(redis)
    for i in range(5):
        await lim.register_failure(f"user{i}@x.io", "9.9.9.9")
    assert await lim.retry_after("fresh@x.io", "9.9.9.9") > 0


async def test_counter_always_gets_an_expiry(redis):
    lim = limiter(redis, window=30)
    await lim.register_failure("a@x.io", "1.1.1.1")
    assert 0 < await redis.ttl("login_fail:email:a@x.io") <= 30


async def test_window_expires(redis):
    lim = limiter(redis, window=1)
    for _ in range(3):
        await lim.register_failure("a@x.io", "1.1.1.1")
    assert await lim.retry_after("a@x.io", "1.1.1.1") > 0
    await asyncio.sleep(1.2)
    assert await lim.retry_after("a@x.io", "1.1.1.1") == 0


async def test_reset_clears_only_the_email_counter(redis):
    lim = limiter(redis)
    for _ in range(3):
        await lim.register_failure("a@x.io", "1.1.1.1")
    await lim.reset("a@x.io")
    assert await redis.get("login_fail:email:a@x.io") is None
    assert await redis.get("login_fail:ip:1.1.1.1") == "3"


async def test_parallel_failures_are_all_counted(redis):
    lim = limiter(redis)
    await asyncio.gather(*(lim.register_failure("a@x.io", "1.1.1.1") for _ in range(50)))
    assert await redis.get("login_fail:email:a@x.io") == "50"
