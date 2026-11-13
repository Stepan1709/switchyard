from pathlib import Path

import pytest

from app.core.config import Settings


def test_secret_is_read_from_file_and_stripped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "pg"
    f.write_text("s3cret\n")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(f))
    assert Settings().postgres_password.get_secret_value() == "s3cret"


def test_file_secret_wins_over_plain_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "pg"
    f.write_text("from-file")
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-env")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(f))
    assert Settings().postgres_password.get_secret_value() == "from-file"


def test_plain_env_used_without_file(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REDIS_PASSWORD", "abc")
    assert Settings().redis_password.get_secret_value() == "abc"


def test_urls_escape_special_characters():
    s = Settings(
        postgres_password="p@ss/word:1",  # type: ignore[arg-type]
        rabbitmq_password="a b",  # type: ignore[arg-type]
        redis_password="x#y",  # type: ignore[arg-type]
    )
    assert "p%40ss%2Fword%3A1@" in s.database_url
    assert "a%20b@" in s.amqp_url
    assert s.redis_url.startswith("redis://:x%23y@")


def test_redis_url_without_password():
    assert Settings().redis_url == "redis://localhost:6379/0"


def test_secrets_are_not_in_repr():
    s = Settings(postgres_password="topsecret")  # type: ignore[arg-type]
    assert "topsecret" not in repr(s)
