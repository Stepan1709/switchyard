"""Real PostgreSQL, Redis and RabbitMQ in containers (the images used by compose)."""

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.api_main import create_app
from app.core.config import Settings

# The Ryuk cleanup sidecar needs an extra image pull; containers are removed by fixtures instead.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

PG_IMAGE = "postgres:17.11-alpine"
REDIS_IMAGE = "redis:8.6.8-alpine"
RABBIT_IMAGE = "rabbitmq:4.3.7-management-alpine"
PASSWORD = "test-password"


@dataclass
class Endpoint:
    host: str
    port: int


def start_postgres() -> PostgresContainer:
    c = PostgresContainer(PG_IMAGE, username="switchyard", password=PASSWORD, dbname="switchyard")
    return c.start()


def endpoint(container: DockerContainer, port: int) -> Endpoint:
    return Endpoint(container.get_container_host_ip(), int(container.get_exposed_port(port)))


def make_settings(pg: Endpoint, redis: Endpoint, rabbit: Endpoint) -> Settings:
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
