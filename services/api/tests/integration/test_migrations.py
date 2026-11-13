import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import Base
from tests.integration.conftest import Endpoint, alembic_config, make_settings

pytestmark = pytest.mark.integration


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
    assert asyncio.run(_version(url)) == "0002"

    command.upgrade(cfg, "head")  # running twice is safe
    assert asyncio.run(_version(url)) == "0002"

    command.downgrade(cfg, "base")
    assert asyncio.run(_version(url)) is None
    command.upgrade(cfg, "head")  # leave the database migrated for other tests


async def _schema_diff(url: str) -> list[object]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:

            def compare(sync_conn):
                return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)

            return list(await conn.run_sync(compare))
    finally:
        await engine.dispose()


def test_models_match_migrations(pg_ep: Endpoint, redis_ep: Endpoint, rabbit_ep: Endpoint):
    """After `upgrade head` the models must describe exactly the migrated schema.

    Catches the classic mistake of changing a model and forgetting the migration.
    """
    import app.modules.audit.models
    import app.modules.auth.models
    import app.modules.projects.models  # noqa: F401

    url = make_settings(pg_ep, redis_ep, rabbit_ep).database_url
    command.upgrade(alembic_config(url), "head")
    assert asyncio.run(_schema_diff(url)) == []
