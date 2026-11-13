"""Health endpoints.

/health/live  - the process is alive (no dependencies are touched).
/health/ready - PostgreSQL, Redis and RabbitMQ are reachable; 503 otherwise, so an
                orchestrator stops sending traffic without restarting the process.
"""

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.logging import get_logger
from app.core.resources import Resources

router = APIRouter(prefix="/health", tags=["health"])
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


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    res: Resources = request.app.state.resources
    timeout = request.app.state.settings.health_check_timeout
    names = ("postgres", "redis", "rabbitmq")
    results = await asyncio.gather(
        _run("postgres", lambda: _check_db(res), timeout),
        _run("redis", lambda: _check_redis(res), timeout),
        _run("rabbitmq", lambda: _check_amqp(res), timeout),
    )
    checks = {n: ("ok" if r is None else "fail") for n, r in zip(names, results, strict=True)}
    ok = all(r is None for r in results)
    return JSONResponse(
        {"status": "ok" if ok else "unavailable", "checks": checks},
        status_code=200 if ok else 503,
    )
