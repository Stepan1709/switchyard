import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import Endpoint, make_settings

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _version(url: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            exists = (await conn.execute(text("SELECT to_regclass('alembic_version')"))).scalar()
            if exists is None:
                return None
            return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    finally:
        await engine.dispose()


# Synchronous tests: Alembic's async env.py calls asyncio.run() itself.
def test_upgrade_and_downgrade(pg_ep: Endpoint, redis_ep: Endpoint, rabbit_ep: Endpoint):
    url = make_settings(pg_ep, redis_ep, rabbit_ep).database_url
    cfg = alembic_config(url)

    command.upgrade(cfg, "head")
    assert asyncio.run(_version(url)) == "0001"

    command.upgrade(cfg, "head")  # running twice is safe
    assert asyncio.run(_version(url)) == "0001"

    command.downgrade(cfg, "base")
    assert asyncio.run(_version(url)) is None
    command.upgrade(cfg, "head")  # leave the database migrated for other tests
