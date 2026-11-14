"""Real PostgreSQL, Redis and RabbitMQ in containers (the images used by compose)."""

import asyncio
import base64
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import aio_pika
import asyncpg
import httpx
import pytest
from aio_pika.abc import AbstractRobustConnection
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from redis.asyncio import Redis
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.api_main import create_app
from app.core.config import Settings
from app.core.messaging import Publisher

# The Ryuk cleanup sidecar needs an extra image pull; containers are removed by fixtures instead.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

PG_IMAGE = "postgres:17.11-alpine"
REDIS_IMAGE = "redis:8.6.8-alpine"
RABBIT_IMAGE = "rabbitmq:4.3.7-management-alpine"
PASSWORD = "test-password"
API_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Endpoint:
    host: str
    port: int


def start_postgres() -> PostgresContainer:
    c = PostgresContainer(PG_IMAGE, username="switchyard", password=PASSWORD, dbname="switchyard")
    return c.start()


def endpoint(container: DockerContainer, port: int) -> Endpoint:
    return Endpoint(container.get_container_host_ip(), int(container.get_exposed_port(port)))


JWT_SECRET = "j" * 40
KEY_PEPPER = "p" * 40
# Two keys: the first encrypts, the second only decrypts (as after a rotation).
MASTER_KEYS = "\n".join(
    [
        "k2:" + base64.b64encode(b"2" * 32).decode(),
        "k1:" + base64.b64encode(b"1" * 32).decode(),
    ]
)
MOCK_KEY = "mock-provider-key-1234"


def make_settings(pg: Endpoint, redis: Endpoint, rabbit: Endpoint, **overrides: object) -> Settings:
    return Settings(
        service_name="api-test",
        postgres_host=pg.host,
        postgres_port=pg.port,
        postgres_password=PASSWORD,  # type: ignore[arg-type]
        redis_host=redis.host,
        redis_port=redis.port,
        redis_password=PASSWORD,  # type: ignore[arg-type]
        rabbitmq_host=rabbit.host,
        rabbitmq_port=rabbit.port,
        rabbitmq_password=PASSWORD,  # type: ignore[arg-type]
        jwt_secret=JWT_SECRET,  # type: ignore[arg-type]
        key_pepper=KEY_PEPPER,  # type: ignore[arg-type]
        master_keys=MASTER_KEYS,  # type: ignore[arg-type]
        # `localhost` stands for a self-hosted provider (the mock server started by a fixture).
        local_provider_hosts=["localhost"],
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    c = start_postgres()
    yield c
    c.stop()


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    c = RedisContainer(REDIS_IMAGE, password=PASSWORD).start()
    yield c
    c.stop()


@pytest.fixture(scope="session")
def rabbit() -> Iterator[DockerContainer]:
    c = (
        DockerContainer(RABBIT_IMAGE)
        .with_env("RABBITMQ_DEFAULT_USER", "switchyard")
        .with_env("RABBITMQ_DEFAULT_PASS", PASSWORD)
        .with_exposed_ports(5672)
        .waiting_for(LogMessageWaitStrategy("Server startup complete").with_startup_timeout(90))
    )
    c.start()
    yield c
    c.stop()


@pytest.fixture(scope="session")
def pg_ep(postgres: PostgresContainer) -> Endpoint:
    return endpoint(postgres, 5432)


@pytest.fixture(scope="session")
def redis_ep(redis_container: RedisContainer) -> Endpoint:
    return endpoint(redis_container, 6379)


@pytest.fixture(scope="session")
def rabbit_ep(rabbit: DockerContainer) -> Endpoint:
    return endpoint(rabbit, 5672)


@pytest.fixture
def settings(pg_ep: Endpoint, redis_ep: Endpoint, rabbit_ep: Endpoint) -> Settings:
    return make_settings(pg_ep, redis_ep, rabbit_ep)


@pytest.fixture
async def running_app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings)
    async with LifespanManager(app):
        yield app


# ---- isolated databases ----
# One migrated template database per session; every test gets its own cheap copy of it, so
# tests never see each other's rows and still run against the real schema.
ADMIN_EMAIL = "root@example.com"
ADMIN_PASSWORD = "correct-horse-battery"


def _admin_dsn(pg: Endpoint, dbname: str = "postgres") -> str:
    return f"postgresql://switchyard:{PASSWORD}@{pg.host}:{pg.port}/{dbname}"


async def _exec(pg: Endpoint, sql: str) -> None:
    conn = await asyncpg.connect(_admin_dsn(pg))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def template_db(pg_ep: Endpoint, redis_ep: Endpoint, rabbit_ep: Endpoint) -> str:
    name = "switchyard_template"
    asyncio.run(_exec(pg_ep, f'CREATE DATABASE "{name}"'))
    settings = make_settings(pg_ep, redis_ep, rabbit_ep, postgres_db=name)
    command.upgrade(alembic_config(settings.database_url), "head")
    return name


@pytest.fixture
async def fresh_db(pg_ep: Endpoint, template_db: str) -> AsyncIterator[str]:
    name = f"db_{uuid.uuid4().hex[:12]}"
    await _exec(pg_ep, f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
    yield name
    await _exec(pg_ep, f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
async def app_settings(
    fresh_db: str, pg_ep: Endpoint, redis_ep: Endpoint, rabbit_ep: Endpoint
) -> Settings:
    # Login counters live in Redis; start every test from a clean slate.
    redis = Redis(host=redis_ep.host, port=redis_ep.port, password=PASSWORD)
    await redis.flushall()
    await redis.aclose()
    return make_settings(
        pg_ep,
        redis_ep,
        rabbit_ep,
        postgres_db=fresh_db,
        admin_email=ADMIN_EMAIL,
        admin_password=ADMIN_PASSWORD,
    )


@pytest.fixture
async def api_app(app_settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(app_settings)
    async with LifespanManager(app):
        yield app


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def alembic_config(url: str) -> Config:
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


# ---- a real mock_llm process ----
MOCK_ROOT = API_ROOT.parent / "mock_llm"


@dataclass
class MockServer:
    base_url: str  # http://localhost:<port>/v1
    port: int


def _start_mock(time_scale: str) -> Iterator[MockServer]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    env = {**os.environ, "MOCK_API_KEY": MOCK_KEY, "MOCK_TIME_SCALE": time_scale}
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "--factory", "app.main:create_app",
            "--host", "127.0.0.1", "--port", str(port), "--no-access-log",
            "--timeout-graceful-shutdown", "1",
        ],
        cwd=MOCK_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("mock_llm did not start")
        yield MockServer(f"http://localhost:{port}/v1", port)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def mock_llm() -> Iterator[MockServer]:
    """The real mock provider in a subprocess: instant answers, requires an API key."""
    yield from _start_mock("0")


@pytest.fixture(scope="session")
def mock_llm_slow() -> Iterator[MockServer]:
    """The same provider with realistic delays (time to first token, tokens per second)."""
    yield from _start_mock("1")


# ---- RabbitMQ helpers ---------------------------------------------------------------------------
@pytest.fixture
async def amqp(rabbit_ep: Endpoint) -> AsyncIterator[AbstractRobustConnection]:
    connection = await aio_pika.connect_robust(
        f"amqp://switchyard:{PASSWORD}@{rabbit_ep.host}:{rabbit_ep.port}/"
    )
    yield connection
    await connection.close()


@pytest.fixture
def queue_name() -> str:
    """A fresh queue name per test: queues are durable and shared by the session's broker."""
    return f"t_{uuid.uuid4().hex[:10]}"


@pytest.fixture
async def publisher(amqp: AbstractRobustConnection) -> Publisher:
    return await Publisher.create(await amqp.channel())


async def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05):  # noqa: ASYNC109
    """Poll `predicate` (sync or async) until it is truthy; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met in time")


async def queue_depth(amqp: AbstractRobustConnection, name: str) -> int:
    """Number of ready messages in a queue (a passive declare does not create or change it)."""
    channel = await amqp.channel()
    try:
        queue = await channel.declare_queue(name, passive=True)
        return int(queue.declaration_result.message_count or 0)
    finally:
        await channel.close()
