"""Long-lived resources created once per process in lifespan and kept in app.state.

Closing order is the reverse of creation: the users of a connection are closed before it.
"""

from dataclasses import dataclass

import aio_pika
import httpx
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class Resources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    amqp_connection: AbstractRobustConnection
    amqp_channel: AbstractRobustChannel
    http: httpx.AsyncClient

    async def aclose(self) -> None:
        # http first (it may be mid-call), then broker, cache, and the database last.
        await self.http.aclose()
        await self.amqp_channel.close()
        await self.amqp_connection.close()
        await self.redis.aclose()
        await self.engine.dispose()
        log.info("resources_closed")


def build_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
            keepalive_expiry=settings.http_keepalive_expiry,
        ),
        timeout=httpx.Timeout(
            connect=settings.http_connect_timeout,
            read=settings.http_read_timeout,
            write=settings.http_write_timeout,
            pool=settings.http_pool_timeout,
        ),
        follow_redirects=False,  # SSRF protection: redirects are never followed.
    )


async def open_resources(settings: Settings) -> Resources:
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # drop dead connections after a database restart
        connect_args={"timeout": settings.db_connect_timeout},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    # decode_responses: values are JSON text; binary payloads are not stored in Redis.
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    connection = await aio_pika.connect_robust(
        settings.amqp_url, client_properties={"connection_name": settings.service_name}
    )
    channel = await connection.channel()  # publisher confirms are on by default
    log.info("resources_opened")
    return Resources(
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        amqp_connection=connection,
        amqp_channel=channel,
        http=build_http_client(settings),
    )
