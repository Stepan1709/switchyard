"""The worker process (real subprocess): health, graceful shutdown, a killed worker."""

import asyncio
import os
import signal
import socket
import sys
import time

import httpx
import pytest
from sqlalchemy import text

from app.core.outbox import enqueue
from tests.integration.conftest import API_ROOT, wait_until

pytestmark = pytest.mark.integration


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Worker:
    def __init__(self, settings, queue: str, **extra_env: str) -> None:
        self.port = free_port()
        self.env = {
            **os.environ,
            "POSTGRES_HOST": settings.postgres_host,
            "POSTGRES_PORT": str(settings.postgres_port),
            "POSTGRES_DB": settings.postgres_db,
            "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
            "REDIS_HOST": settings.redis_host,
            "REDIS_PORT": str(settings.redis_port),
            "REDIS_PASSWORD": settings.redis_password.get_secret_value(),
            "RABBITMQ_HOST": settings.rabbitmq_host,
            "RABBITMQ_PORT": str(settings.rabbitmq_port),
            "RABBITMQ_PASSWORD": settings.rabbitmq_password.get_secret_value(),
            "WORKER_PORT": str(self.port),
            "ECHO_QUEUE": queue,
            "RETRY_DELAYS_MS": "200,400",
            "OUTBOX_POLL_INTERVAL": "0.1",
            "CONSUMER_PREFETCH": "1",
            **extra_env,
        }
        self.proc: asyncio.subprocess.Process | None = None
        self.url = f"http://127.0.0.1:{self.port}"

    async def start(self) -> "Worker":
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.integration.echo_worker",
            cwd=API_ROOT, env=self.env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        async with httpx.AsyncClient() as client:

            async def ready() -> bool:
                try:
                    return (await client.get(f"{self.url}/health/ready")).status_code == 200
                except httpx.TransportError:
                    return False

            await wait_until(ready, timeout=30)
        return self

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    async def stop(self) -> int:
        """SIGTERM, wait for exit; returns the exit code."""
        assert self.proc is not None
        if self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
        return await asyncio.wait_for(self.proc.wait(), 15)

    async def kill(self) -> None:
        assert self.proc is not None
        self.proc.kill()
        await self.proc.wait()

    async def output(self) -> str:
        assert self.proc is not None and self.proc.stdout is not None
        return (await self.proc.stdout.read()).decode(errors="replace")

    async def cleanup(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


@pytest.fixture
async def tables(api_app):
    factory = api_app.state.resources.session_factory
    async with factory() as s, s.begin():
        await s.execute(text("CREATE TABLE echo_started (message_id uuid, pid int, attempt int)"))
        await s.execute(text("CREATE TABLE echo_results (message_id uuid, pid int)"))
    return factory


@pytest.fixture
async def workers():
    started: list[Worker] = []
    yield started
    for w in started:
        await w.cleanup()


async def launch(workers, settings, queue, **env) -> Worker:
    worker = Worker(settings, queue, **env)
    workers.append(worker)
    return await worker.start()


async def emit(factory, payload: dict, n: int = 1) -> list[str]:
    ids = []
    async with factory() as s, s.begin():
        for _ in range(n):
            ids.append(str(enqueue(s, "echo.event", payload)))
    return ids


async def rows(factory, sql: str):
    async with factory() as s:
        return (await s.execute(text(sql))).all()


async def test_health_endpoints_report_consumers_and_the_relay(
    app_settings, tables, workers, queue_name
):
    worker = await launch(workers, app_settings, queue_name)
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"{worker.url}/health/live")).json() == {"status": "ok"}
        ready = await client.get(f"{worker.url}/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {
        "postgres": "ok",
        "redis": "ok",
        "rabbitmq": "ok",
        "consumers": "ok",
        "outbox_relay": "ok",
    }
    assert await worker.stop() == 0


async def test_an_event_travels_from_the_outbox_through_the_relay_to_the_consumer(
    app_settings, tables, workers, queue_name
):
    await launch(workers, app_settings, queue_name)
    ids = await emit(tables, {}, n=3)
    await wait_until(lambda: _count(tables, "echo_results", 3))
    results = {str(r.message_id) for r in await rows(tables, "SELECT message_id FROM echo_results")}
    assert results == set(ids)
    assert await rows(tables, "SELECT 1 FROM outbox WHERE sent_at IS NULL") == []


async def _count(factory, table: str, expected: int) -> bool:
    async with factory() as s:
        return (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == expected


async def test_temporary_failures_are_retried_by_the_worker(
    app_settings, tables, workers, queue_name
):
    await launch(workers, app_settings, queue_name)
    await emit(tables, {"fail_times": 2})
    await wait_until(lambda: _count(tables, "echo_results", 1))
    attempts = [
        r.attempt for r in await rows(tables, "SELECT attempt FROM echo_started ORDER BY attempt")
    ]
    assert attempts == [0, 1, 2]  # two failures, then success


async def test_sigterm_lets_the_running_message_finish_and_exits_cleanly(
    app_settings, tables, workers, queue_name
):
    worker = await launch(workers, app_settings, queue_name)
    await emit(tables, {"sleep": 1.5})
    await wait_until(lambda: _count(tables, "echo_started", 1))  # the handler is running
    started = time.monotonic()

    async with httpx.AsyncClient() as client:
        assert worker.proc is not None
        worker.proc.send_signal(signal.SIGTERM)
        draining = await wait_until(
            lambda: _status(client, f"{worker.url}/health/ready", 503), timeout=3
        )
    assert draining  # readiness drops while the worker drains

    code = await asyncio.wait_for(worker.proc.wait(), 15)
    assert code == 0 and time.monotonic() - started < 10
    assert await _count(tables, "echo_results", 1)  # the in-flight message was finished and acked
    assert "worker_stopped" in await worker.output()


async def _status(client, url, expected) -> bool:
    try:
        return (await client.get(url)).status_code == expected
    except httpx.TransportError:
        return False


async def test_a_killed_worker_loses_nothing_another_worker_processes_the_message(
    app_settings, tables, workers, queue_name
):
    first = await launch(workers, app_settings, queue_name)
    second = await launch(workers, app_settings, queue_name)
    by_pid = {first.pid: first, second.pid: second}

    [message_id] = await emit(tables, {"sleep": 2.5})
    started = await wait_until(lambda: rows(tables, "SELECT pid FROM echo_started"))
    victim = by_pid[started[0].pid]
    survivor = second if victim is first else first
    await victim.kill()  # SIGKILL in the middle of handling: no ack, no cleanup

    await wait_until(lambda: _count(tables, "echo_results", 1), timeout=30)
    done = await rows(tables, "SELECT message_id, pid FROM echo_results")
    assert [str(r.message_id) for r in done] == [message_id]  # exactly once
    assert done[0].pid == survivor.pid  # finished by the other worker
    assert {r.pid for r in await rows(tables, "SELECT pid FROM echo_started")} == {
        first.pid,
        second.pid,
    }
    assert await survivor.stop() == 0


async def test_the_relay_survives_a_worker_restart(app_settings, tables, workers, queue_name):
    first = await launch(workers, app_settings, queue_name)
    assert await first.stop() == 0

    ids = await emit(tables, {}, n=5)  # events are written while no worker is running
    assert len(await rows(tables, "SELECT 1 FROM outbox WHERE sent_at IS NULL")) == 5

    await launch(workers, app_settings, queue_name)  # a new process starts
    await wait_until(lambda: _count(tables, "echo_results", 5), timeout=30)
    results = {str(r.message_id) for r in await rows(tables, "SELECT message_id FROM echo_results")}
    assert results == set(ids)
    assert await rows(tables, "SELECT 1 FROM outbox WHERE sent_at IS NULL") == []
