"""The web interface against the REAL api (a separate process) on real Postgres, Redis, RabbitMQ.

The fake api of the unit tests can drift from the real one; these tests catch that. The api runs
from the virtualenv of `services/api` (run `uv sync` there first), exactly as in production:
its own process, spoken to over HTTP.
"""

import base64
import os
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.config import Settings
from app.main import create_app

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

PG_IMAGE = "postgres:17.11-alpine"
REDIS_IMAGE = "redis:8.6.8-alpine"
RABBIT_IMAGE = "rabbitmq:4.3.7-management-alpine"
PASSWORD = "test-password"
ADMIN_EMAIL = "root@example.com"
ADMIN_PASSWORD = "correct-horse-battery"
API_ROOT = Path(__file__).resolve().parents[3] / "api"
API_PYTHON = API_ROOT / ".venv" / "bin" / "python"


@dataclass
class Stack:
    api_url: str
    redis_host: str
    redis_port: int


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    if not API_PYTHON.exists():
        pytest.skip(f"the api virtualenv is missing ({API_PYTHON}): run `uv sync` in services/api")
    postgres = PostgresContainer(
        PG_IMAGE, username="switchyard", password=PASSWORD, dbname="switchyard"
    ).start()
    redis = RedisContainer(REDIS_IMAGE, password=PASSWORD).start()
    rabbit = (
        DockerContainer(RABBIT_IMAGE)
        .with_env("RABBITMQ_DEFAULT_USER", "switchyard")
        .with_env("RABBITMQ_DEFAULT_PASS", PASSWORD)
        .with_exposed_ports(5672)
        .waiting_for(LogMessageWaitStrategy("Server startup complete").with_startup_timeout(90))
    )
    rabbit.start()
    process: subprocess.Popen[bytes] | None = None
    try:
        port = _free_port()
        env = {
            **os.environ,
            "POSTGRES_HOST": postgres.get_container_host_ip(),
            "POSTGRES_PORT": str(postgres.get_exposed_port(5432)),
            "POSTGRES_PASSWORD": PASSWORD,
            "REDIS_HOST": redis.get_container_host_ip(),
            "REDIS_PORT": str(redis.get_exposed_port(6379)),
            "REDIS_PASSWORD": PASSWORD,
            "RABBITMQ_HOST": rabbit.get_container_host_ip(),
            "RABBITMQ_PORT": str(rabbit.get_exposed_port(5672)),
            "RABBITMQ_PASSWORD": PASSWORD,
            "JWT_SECRET": "j" * 40,
            "KEY_PEPPER": "p" * 40,
            "MASTER_KEYS": "k1:" + base64.b64encode(b"1" * 32).decode(),
            "ADMIN_EMAIL": ADMIN_EMAIL,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "LOCAL_PROVIDER_HOSTS": "localhost",
            "LOGIN_MAX_FAILURES_PER_IP": "1000",
        }
        migrated = subprocess.run(
            [str(API_PYTHON), "-m", "alembic", "upgrade", "head"],
            cwd=API_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        process = subprocess.Popen(
            [str(API_PYTHON), "-m", "uvicorn", "--factory", "app.api_main:create_app",
             "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
            cwd=API_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/health/ready", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("the api did not become ready")
        yield Stack(url, redis.get_container_host_ip(), int(redis.get_exposed_port(6379)))
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        for container in (rabbit, redis, postgres):
            container.stop()


@pytest.fixture
async def web(stack: Stack) -> AsyncIterator[FastAPI]:
    settings = Settings(
        api_url=stack.api_url,
        redis_host=stack.redis_host,
        redis_port=stack.redis_port,
        redis_password=PASSWORD,  # type: ignore[arg-type]
        cookie_secure=False,
        env="test",
    )
    app = create_app(settings)
    async with LifespanManager(app):
        yield app


@pytest.fixture
async def browser(web: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A person with a browser: keeps cookies, does not follow redirects by itself."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web), base_url="http://web", follow_redirects=False
    ) as c:
        yield c
