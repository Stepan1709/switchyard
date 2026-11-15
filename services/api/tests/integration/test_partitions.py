"""Creating and dropping the monthly partitions of the traffic log."""

import asyncio
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from app.modules.traffic import partitions as p

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)


@pytest.fixture
def factory(api_app):
    return api_app.state.resources.session_factory


async def names(factory) -> list[str]:
    async with factory() as s:
        return await p.list_partitions(s)


async def test_the_migration_created_a_usable_window(factory):
    existing = await names(factory)
    today = p.month_start(datetime.now(UTC))
    for delta in (-1, 0, 1, 2):
        assert p.partition_name(p.add_months(today, delta)) in existing


async def test_ensure_window_is_idempotent_and_race_safe(factory):
    created = await asyncio.gather(
        *(p.ensure_window(factory, NOW, ahead=3, behind=2) for _ in range(5))
    )
    assert all(c == created[0] for c in created)
    expected = {f"requests_2026_{m:02d}" for m in (9, 10, 11, 12)} | {
        "requests_2027_01",
        "requests_2027_02",
    }
    assert set(created[0]) == expected
    assert expected <= set(await names(factory))
    await p.ensure_window(factory, NOW, ahead=3, behind=2)  # a second run changes nothing


async def test_rows_land_in_the_partition_of_their_month(factory):
    await p.ensure_window(factory, NOW, ahead=1, behind=1)
    ids = {m: uuid.uuid4() for m in (10, 11)}
    async with factory() as s, s.begin():
        for month, rid in ids.items():
            await s.execute(
                text(
                    "INSERT INTO requests (request_id, started_at, project_id, mode, stream, status)"
                    " VALUES (:r, :t, gen_random_uuid(), 'record', false, 'ok')"
                ),
                {"r": rid, "t": datetime(2026, month, 20, tzinfo=UTC)},
            )
        where = {
            m: (
                await s.execute(
                    text(f"SELECT count(*) FROM requests_2026_{m:02d} WHERE request_id = :r"),
                    {"r": rid},
                )
            ).scalar_one()
            for m, rid in ids.items()
        }
    assert where == {10: 1, 11: 1}


async def test_expired_partitions_are_dropped_with_their_rows(factory):
    await p.ensure_window(factory, NOW, ahead=1, behind=8)  # 2026-03 .. 2026-12
    async with factory() as s, s.begin():
        for month in (3, 8, 9, 11):
            await s.execute(
                text(
                    "INSERT INTO requests (request_id, started_at, project_id, mode, stream, status)"
                    " VALUES (gen_random_uuid(), :t, gen_random_uuid(), 'record', false, 'ok')"
                ),
                {"t": datetime(2026, month, 5, tzinfo=UTC)},
            )
    dropped = await p.drop_expired(factory, NOW, retention_months=3)
    # now = November, retention 3 months: keep August (the cutoff month) .. and everything later
    assert dropped == [f"requests_2026_{m:02d}" for m in range(3, 8)]
    remaining = await names(factory)
    assert "requests_2026_08" in remaining and "requests_2026_03" not in remaining
    async with factory() as s:
        months = [
            r[0].month
            for r in await s.execute(
                text(
                    "SELECT started_at FROM requests WHERE started_at < '2027-01-01' ORDER BY started_at"
                )
            )
            if r[0].year == 2026
        ]
    assert 3 not in months and {8, 9, 11} <= set(months)  # the old row went with its partition


async def test_dropping_never_touches_foreign_tables_or_the_parent(factory):
    async with factory() as s, s.begin():
        await s.execute(text("CREATE TABLE requests_backup (x int)"))
        await s.execute(
            text("CREATE TABLE requests_2020_01 (x int)")
        )  # looks like a partition, is not
    await p.drop_expired(factory, NOW, retention_months=3)
    async with factory() as s:
        found = {
            r[0]
            for r in await s.execute(
                text(
                    "SELECT relname FROM pg_class WHERE relname IN ('requests', 'requests_backup', 'requests_2020_01')"
                )
            )
        }
    assert found == {"requests", "requests_backup", "requests_2020_01"}


async def test_retention_arithmetic_across_the_year_boundary(factory):
    january = datetime(2027, 1, 10, tzinfo=UTC)
    await p.ensure_window(factory, january, ahead=0, behind=6)
    dropped = await p.drop_expired(factory, january, retention_months=3)
    assert "requests_2026_09" in dropped and "requests_2026_10" not in dropped
    assert date(2026, 10, 1) == p.add_months(p.month_start(january), -3)
