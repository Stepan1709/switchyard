"""Entry point of the gateway process: `uvicorn --factory app.gateway_main:create_app`."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import metrics, ssrf, tracing
from app.core.config import Settings
from app.core.crypto import KeyRing
from app.core.logging import configure_logging, get_logger
from app.core.snapshot import SnapshotStore
from app.modules.gateway.budget import BudgetGuard
from app.modules.gateway.errors import GatewayError
from app.modules.gateway.events import TrafficEventPublisher
from app.modules.gateway.proxy import GatewayProxy
from app.modules.gateway.ratelimit import RateLimiter
from app.modules.gateway.record import CompositeRecorder, LogRecorder, Recorder
from app.modules.gateway.router import router
from app.modules.gateway.rules_listener import RulesListener
from app.modules.gateway.telemetry import MetricsRecorder

log = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    recorder: Recorder | None = None,
    resolver: ssrf.Resolver | None = None,
) -> FastAPI:
    settings = settings or Settings(service_name="gateway")
    settings.require_gateway_secrets()
    configure_logging(settings.service_name, settings.env, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Pools and clients are created once and reused for every request.
        engine = create_async_engine(
            settings.database_url,
            pool_size=2,  # only the snapshot refresh uses the database
            max_overflow=0,
            pool_pre_ping=True,
            connect_args={"timeout": settings.db_connect_timeout},
        )
        redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        http = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=settings.http_max_connections * 10,
                max_keepalive_connections=settings.http_max_keepalive * 5,
                keepalive_expiry=settings.http_keepalive_expiry,
            ),
            follow_redirects=False,  # SSRF protection
        )
        store = SnapshotStore(
            async_sessionmaker(engine, expire_on_commit=False),
            redis,
            KeyRing.from_text(settings.master_keys.get_secret_value()),
            refresh_seconds=settings.snapshot_refresh_seconds,
        )
        events = TrafficEventPublisher(settings)
        rules_listener = RulesListener(settings, store.refresh)
        app.state.rules_listener = rules_listener
        app.state.events = events
        app.state.settings = settings
        app.state.engine = engine  # exposed so tests can count database statements
        app.state.proxy = GatewayProxy(
            settings,
            store,
            http,
            RateLimiter(redis),
            recorder
            or CompositeRecorder(
                LogRecorder(settings.gateway_log_requests), MetricsRecorder(), events
            ),
            resolver or ssrf.CachingResolver(settings.gateway_resolve_cache_ttl),
            BudgetGuard(redis),
        )
        # Read at scrape time: nothing runs on the request path for these two.
        metrics.SNAPSHOT_AGE.set_function(lambda: time.monotonic() - store.confirmed_at)
        metrics.LOG_EVENTS_BUFFERED.set_function(lambda: events.buffered)
        await store.refresh()  # so that the first request already finds a snapshot
        refresher = asyncio.create_task(store.run(), name="snapshot-refresh")
        sender = asyncio.create_task(events.run(), name="traffic-events")
        listener = asyncio.create_task(rules_listener.run(), name="rules-listener")
        log.info("gateway_started", snapshot=store.current is not None)
        try:
            yield
        finally:
            log.info("gateway_stopping")
            await events.stop()  # try to flush buffered events before the connections close
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(sender, 5)
            rules_listener.stop()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(listener, 5)
            store.stop()
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Switchyard gateway",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(router)
    metrics.add_metrics_endpoint(app, "gateway")
    tracing.setup(settings, app)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # unknown path or method: still an OpenAI-shaped error
        kind = "invalid_request_error" if exc.status_code < 500 else "server_error"
        return GatewayError(exc.status_code, str(exc.detail), kind, "not_found").response()

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("gateway_unhandled_exception", exc_info=exc)
        return GatewayError(
            500, "The gateway hit an internal error.", "server_error", "internal_error"
        ).response()

    return app
