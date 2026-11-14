"""The worker process: queue consumers, the outbox relay, periodic cleanup and a tiny HTTP
server with health checks. One image, started with `python -m app.worker_main`.

Graceful shutdown on SIGTERM / SIGINT (in this order):
  1. readiness turns to 503 (the process is draining);
  2. consumers are cancelled (no new messages) and running handlers get up to
     `worker_shutdown_timeout` seconds to finish; whatever is still unacked goes back to the
     queue and is handled by another worker;
  3. the outbox relay finishes its current batch;
  4. the health server stops and the connections are closed.
"""

import asyncio
import contextlib
import signal
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.core.health import check_dependencies
from app.core.idempotency import purge_expired
from app.core.inbox import purge_processed
from app.core.logging import configure_logging, get_logger
from app.core.messaging import Consumer, ConsumerSpec, Publisher
from app.core.outbox import OutboxRelay
from app.core.resources import Resources, open_resources

log = get_logger(__name__)

SpecsFactory = Callable[[Resources, Settings], list[ConsumerSpec]]
MAINTENANCE_INTERVAL = 3600.0


@dataclass
class WorkerState:
    consumers: list[Consumer] = field(default_factory=list)
    relay: OutboxRelay | None = None
    draining: bool = False


def create_health_app(res: Resources, settings: Settings, state: WorkerState) -> FastAPI:
    app = FastAPI(title="Switchyard worker", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        checks = await check_dependencies(res, settings.health_check_timeout)
        checks["consumers"] = "ok" if all(c.running for c in state.consumers) else "fail"
        if state.relay is not None:
            loop_time = asyncio.get_running_loop().time()
            # The relay marks every loop iteration; silence means it is stuck or dead.
            alive = loop_time - state.relay.last_tick < settings.outbox_poll_interval * 10 + 5
            checks["outbox_relay"] = "ok" if alive else "fail"
        ok = all(v == "ok" for v in checks.values()) and not state.draining
        return JSONResponse(
            {"status": "ok" if ok else "unavailable", "checks": checks},
            status_code=200 if ok else 503,
        )

    return app


class _QuietServer(uvicorn.Server):
    """uvicorn without its own signal handling: the worker owns SIGTERM/SIGINT."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _maintenance(res: Resources, settings: Settings, stop: asyncio.Event) -> None:
    """Hourly cleanup of expired idempotency keys and old inbox records."""
    while not stop.is_set():
        try:
            keys = await purge_expired(res.session_factory)
            inbox = await purge_processed(res.session_factory, settings.processed_retention_days)
            log.info("maintenance_done", idempotency_keys=keys, processed_messages=inbox)
        except Exception as exc:
            log.error("maintenance_failed", error=type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), MAINTENANCE_INTERVAL)


async def run_worker(
    settings: Settings,
    specs_factory: SpecsFactory,
    *,
    stop: asyncio.Event | None = None,
    handle_signals: bool = True,
    ready: asyncio.Event | None = None,
) -> None:
    """Run until `stop` is set (or SIGTERM/SIGINT arrives)."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if handle_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

    res = await open_resources(settings)
    state = WorkerState()
    publisher = await Publisher.create(res.amqp_channel)
    try:
        for spec in specs_factory(res, settings):
            consumer = Consumer(
                spec,
                res.amqp_connection,
                publisher,
                retry_delays_ms=settings.retry_delays_ms,
                delivery_limit=settings.delivery_limit,
                default_prefetch=settings.consumer_prefetch,
            )
            await consumer.start()
            state.consumers.append(consumer)

        state.relay = OutboxRelay(
            res.session_factory,
            publisher,
            poll_interval=settings.outbox_poll_interval,
            batch_size=settings.outbox_batch_size,
            unroutable_attempts=settings.outbox_unroutable_attempts,
        )
        relay_task = asyncio.create_task(state.relay.run(), name="outbox-relay")
        maintenance_task = asyncio.create_task(_maintenance(res, settings, stop))

        config = uvicorn.Config(
            create_health_app(res, settings, state),
            host="0.0.0.0",
            port=settings.worker_port,
            log_config=None,
            access_log=False,
        )
        server = _QuietServer(config)
        server_task = asyncio.create_task(server.serve(), name="health-server")

        log.info("worker_started", consumers=[c.spec.name for c in state.consumers])
        if ready is not None:
            ready.set()
        await stop.wait()

        log.info("worker_stopping")
        state.draining = True
        started = time.monotonic()
        await asyncio.gather(
            *(c.stop(wait=settings.worker_shutdown_timeout) for c in state.consumers)
        )
        state.relay.stop()
        await relay_task
        maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance_task
        server.should_exit = True
        await server_task
        log.info("worker_stopped", seconds=round(time.monotonic() - started, 2))
    finally:
        await res.aclose()
        if handle_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)


def main(specs_factory: SpecsFactory) -> None:
    settings = Settings(service_name="worker")
    configure_logging(settings.service_name, settings.env, settings.log_level)
    asyncio.run(run_worker(settings, specs_factory))
