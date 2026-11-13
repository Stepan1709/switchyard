"""Dependencies that hand out lifespan resources to endpoints."""

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from aio_pika.abc import AbstractRobustChannel
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.resources import Resources


def _resources(request: Request) -> Resources:
    res: Resources = request.app.state.resources
    return res


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # One session per request: opened here, always closed (rolled back if not committed).
    async with _resources(request).session_factory() as session:
        yield session


def get_redis(request: Request) -> Redis:
    return _resources(request).redis


def get_http_client(request: Request) -> httpx.AsyncClient:
    return _resources(request).http


def get_amqp_channel(request: Request) -> AbstractRobustChannel:
    return _resources(request).amqp_channel


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
