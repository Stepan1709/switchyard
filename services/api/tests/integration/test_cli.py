import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.cli import main
from app.core.config import Settings
from tests.integration.conftest import PASSWORD, Endpoint

pytestmark = pytest.mark.integration


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, fresh_db: str, pg_ep: Endpoint) -> Settings:
    monkeypatch.setenv("POSTGRES_HOST", pg_ep.host)
    monkeypatch.setenv("POSTGRES_PORT", str(pg_ep.port))
    monkeypatch.setenv("POSTGRES_DB", fresh_db)
    monkeypatch.setenv("POSTGRES_PASSWORD", PASSWORD)
    return Settings()


async def _admins(settings: Settings) -> list[tuple[str, str]]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT email, role FROM admins ORDER BY email"))
            return [(r[0], r[1]) for r in rows]
    finally:
        await engine.dispose()


async def test_create_admin_from_password_file(cli_env: Settings, tmp_path: Path):
    pw = tmp_path / "pw"
    pw.write_text("a-very-long-password\n")
    # main() runs its own event loop, so it is called from a worker thread.
    code = await asyncio.to_thread(
        main, ["create-admin", "--email", "Boss@Example.com", "--password-file", str(pw)]
    )
    assert code == 0
    assert await _admins(cli_env) == [("boss@example.com", "admin")]


async def test_create_viewer_and_reject_duplicates_and_weak_passwords(
    cli_env: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    good, weak = tmp_path / "good", tmp_path / "weak"
    good.write_text("a-very-long-password")
    weak.write_text("short")
    args = ["create-admin", "--email", "v@example.com", "--role", "viewer"]
    assert await asyncio.to_thread(main, [*args, "--password-file", str(good)]) == 0
    assert await asyncio.to_thread(main, [*args, "--password-file", str(good)]) == 1  # duplicate
    assert "already exists" in capsys.readouterr().err
    code = await asyncio.to_thread(
        main, ["create-admin", "--email", "w@example.com", "--password-file", str(weak)]
    )
    assert code == 1 and "at least" in capsys.readouterr().err
    assert await _admins(cli_env) == [("v@example.com", "viewer")]
