"""Calibration with real worker processes: a worker killed in the middle of a run loses no job and
duplicates none; another worker finishes the run."""

import asyncio
import os
import signal
import sys
import uuid

import httpx
import pytest

from app.core.messaging.topology import QueueNames
from tests.integration.calibration_support import Env, build_env, get_run, results, sql, start_run
from tests.integration.conftest import API_ROOT, MASTER_KEYS, MockServer, wait_until
from tests.integration.test_auth_api import bearer, login
from tests.integration.test_worker import free_port

pytestmark = pytest.mark.integration


class WorkerProcess:
    """`python -m app.worker_main`, exactly as the container runs it."""

    def __init__(self, settings, mock_port: int, **queues: str) -> None:
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
            "MASTER_KEYS": MASTER_KEYS,
            "LOCAL_PROVIDER_HOSTS": "localhost",
            "WORKER_PORT": str(self.port),
            "RETRY_DELAYS_MS": "200,400",
            "OUTBOX_POLL_INTERVAL": "0.1",
            "REPLAY_PREFETCH": "4",
            "CALIBRATION_LIMIT_WAIT": "1",
            "CALIBRATION_SLOT_TTL": "3",
            "REPLAY_QUEUE": queues["replay"],
            "RUN_FINISHED_QUEUE": queues["done"],
            "JUDGE_QUEUE": queues["judge"],
            "REQUEST_LOG_QUEUE": queues["log"],
        }
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> "WorkerProcess":
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.worker_main",
            cwd=API_ROOT, env=self.env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        async with httpx.AsyncClient() as client:

            async def ready() -> bool:
                try:
                    r = await client.get(f"http://127.0.0.1:{self.port}/health/ready")
                    return r.status_code == 200
                except httpx.TransportError:
                    return False

            await wait_until(ready, timeout=40)
        return self

    def kill(self) -> None:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGKILL)  # no goodbye: no ack, no cleanup

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 20)
            except TimeoutError:
                self.proc.kill()


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def queues(amqp, queue_name):
    names = {
        "replay": f"{queue_name}_replay",
        "done": f"{queue_name}_done",
        "log": f"{queue_name}_log",
        "judge": f"{queue_name}_judge",
    }
    yield names
    channel = await amqp.channel()
    for name in names.values():
        q = QueueNames(name)
        for queue in [q.work, q.dlq, *(q.retry(i) for i in (1, 2))]:
            await channel.queue_delete(queue)


@pytest.fixture
async def env(client, admin, api_app, mock_llm_slow: MockServer) -> Env:
    env = await build_env(client, admin, api_app, mock_llm_slow.base_url, requests=10)
    # generous limits, set through the API like an administrator would
    r = await client.patch(
        f"/providers/{env.stack.provider['id']}",
        json={"calibration_max_concurrency": 50, "calibration_max_rps": "1000"},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    return env


async def test_a_run_is_completed_by_real_worker_processes(
    client, env, api_app, app_settings, queues, mock_llm_slow
):
    worker = await WorkerProcess(app_settings, mock_llm_slow.port, **queues).start()
    try:
        run = (await start_run(client, env, "tiny", "mid", sample_size=6)).json()

        async def finished():
            return (await get_run(client, env, run["id"]))["status"] == "completed"

        await wait_until(finished, timeout=60)
        rows = await results(api_app, run["id"])
        assert len(rows) == 12 and {r.status for r in rows} == {"ok"}
    finally:
        await worker.stop()


async def test_a_killed_worker_loses_no_job_and_duplicates_none(
    client, env, api_app, app_settings, queues, mock_llm_slow
):
    first = await WorkerProcess(app_settings, mock_llm_slow.port, **queues).start()
    second = await WorkerProcess(app_settings, mock_llm_slow.port, **queues).start()
    try:
        run = (await start_run(client, env, "tiny", "mid", sample_size=10)).json()
        total = run["progress"]["total_jobs"]
        assert total == 20

        # let both workers get going, then kill one while it is holding jobs
        await wait_until(lambda: results(api_app, run["id"]), timeout=40)
        first.kill()
        assert await asyncio.wait_for(first.proc.wait(), 10) == -signal.SIGKILL
        before = len(await results(api_app, run["id"]))
        assert before < total, "the run must still have been in progress at the kill"

        async def finished():
            return (await get_run(client, env, run["id"]))["status"] == "completed"

        await wait_until(finished, timeout=90)
        rows = await results(api_app, run["id"])
        assert len(rows) == total  # nothing was lost ...
        assert len({(r.request_id, r.model_id) for r in rows}) == total  # ... nor duplicated
        assert {r.status for r in rows} == {"ok"}
        done = await sql(
            api_app,
            "SELECT count(*) AS n FROM outbox WHERE type = 'calibration.run.finished'",
        )
        assert done[0].n == 1  # and the end of the run was announced once
    finally:
        await second.stop()
        await first.stop()


async def test_the_budget_holds_across_several_workers(
    client, env, api_app, app_settings, queues, mock_llm_slow
):
    first = await WorkerProcess(app_settings, mock_llm_slow.port, **queues).start()
    second = await WorkerProcess(app_settings, mock_llm_slow.port, **queues).start()
    try:
        run = (await start_run(client, env, "tiny", "mid", sample_size=10, max_requests=7)).json()

        async def stopped():
            return (await get_run(client, env, run["id"]))["status"] == "budget_exceeded"

        await wait_until(stopped, timeout=60)
        await asyncio.sleep(1.0)  # in-flight calls finish, nothing new starts
        rows = await results(api_app, run["id"])
        assert len(rows) == 7  # the budget counters are shared through Redis
        assert uuid.UUID(run["id"])
    finally:
        await second.stop()
        await first.stop()
