from contextlib import suppress

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from testcontainers.community.redis import RedisContainer

from app.api_main import create_app
from tests.integration.conftest import (
    PASSWORD,
    REDIS_IMAGE,
    Endpoint,
    endpoint,
    make_settings,
    start_postgres,
)

pytestmark = pytest.mark.integration


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_live_and_ready_ok(running_app):
    async with client_for(running_app) as c:
        assert (await c.get("/health/live")).json() == {"status": "ok"}
        r = await c.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "checks": {"postgres": "ok", "redis": "ok", "rabbitmq": "ok"},
    }


async def test_session_dependency_runs_queries(running_app):
    res = running_app.state.resources
    async with res.session_factory() as session:
        assert (await session.execute(text("SELECT 41 + 1"))).scalar_one() == 42


async def test_ready_fails_when_database_is_down(redis_ep: Endpoint, rabbit_ep: Endpoint):
    pg = start_postgres()
    try:
        settings = make_settings(endpoint(pg, 5432), redis_ep, rabbit_ep)
        app = create_app(settings)
        async with LifespanManager(app), client_for(app) as c:
            assert (await c.get("/health/ready")).status_code == 200
            pg.stop()
            r = await c.get("/health/ready")
            live = await c.get("/health/live")
        assert r.status_code == 503
        assert r.json()["checks"] == {"postgres": "fail", "redis": "ok", "rabbitmq": "ok"}
        assert live.status_code == 200  # the process itself is still alive
    finally:
        with suppress(Exception):  # already stopped by the test itself
            pg.stop()


async def test_ready_fails_when_redis_is_down(pg_ep: Endpoint, rabbit_ep: Endpoint):
    rc = RedisContainer(REDIS_IMAGE, password=PASSWORD).start()
    try:
        settings = make_settings(pg_ep, endpoint(rc, 6379), rabbit_ep)
        app = create_app(settings)
        async with LifespanManager(app), client_for(app) as c:
            assert (await c.get("/health/ready")).status_code == 200
            rc.stop()
            r = await c.get("/health/ready")
        assert r.status_code == 503 and r.json()["checks"]["redis"] == "fail"
    finally:
        with suppress(Exception):
            rc.stop()


async def test_dependencies_hand_out_working_resources(running_app):
    from sqlalchemy import text

    from app.core.deps import HttpClientDep, RedisDep, SessionDep, get_amqp_channel

    @running_app.get("/_deps")
    async def deps(session: SessionDep, redis: RedisDep, http: HttpClientDep) -> dict[str, object]:
        await redis.set("deps-test", "1", ex=5)
        return {
            "db": (await session.execute(text("SELECT 1"))).scalar_one(),
            "redis": await redis.get("deps-test"),
            "http_follows_redirects": http.follow_redirects,
        }

    async with client_for(running_app) as c:
        r = await c.get("/_deps")
    assert r.json() == {"db": 1, "redis": "1", "http_follows_redirects": False}

    class _Req:  # get_amqp_channel only needs request.app.state
        app = running_app

    assert not get_amqp_channel(_Req()).is_closed  # type: ignore[arg-type]
