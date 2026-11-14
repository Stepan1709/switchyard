"""Dependency checks shared by the readiness probes of api and worker."""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from app.core.logging import get_logger
from app.core.resources import Resources

log = get_logger(__name__)


async def _check_db(res: Resources) -> None:
    async with res.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis(res: Resources) -> None:
    await res.redis.ping()


async def _check_amqp(res: Resources) -> None:
    # The robust connection clears `connected` while it reconnects, so this reflects reality
    # without an extra network round trip.
    conn, ch = res.amqp_connection, res.amqp_channel
    if conn.is_closed or ch.is_closed or not conn.connected.is_set():
        raise ConnectionError("broker connection is not established")


async def _run(name: str, check: Callable[[], Awaitable[None]], limit: float) -> str | None:
    """Returns None when healthy, otherwise a short error class name (no details leak out)."""
    try:
        async with asyncio.timeout(limit):
            await check()
    except Exception as exc:  # any failure of a dependency means "not ready"
        log.warning("readiness_check_failed", dependency=name, error=type(exc).__name__)
        return type(exc).__name__
    return None


async def check_dependencies(res: Resources, limit: float) -> dict[str, str]:
    """Check PostgreSQL, Redis and RabbitMQ in parallel: {"postgres": "ok" | "fail", ...}."""
    names = ("postgres", "redis", "rabbitmq")
    results = await asyncio.gather(
        _run("postgres", lambda: _check_db(res), limit),
        _run("redis", lambda: _check_redis(res), limit),
        _run("rabbitmq", lambda: _check_amqp(res), limit),
    )
    return {n: ("ok" if r is None else "fail") for n, r in zip(names, results, strict=True)}
