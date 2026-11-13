"""Application settings.

Non-secret values come from environment variables. A secret NAME can be given either as
NAME (plain, for tests) or as NAME_FILE (path to a file, e.g. /run/secrets/name): the file
variant is what Docker Compose secrets use, so passwords never appear in `environment`.
"""

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

SECRET_FIELDS = ("postgres_password", "redis_password", "rabbitmq_password")


class _FileSecretsSource(PydanticBaseSettingsSource):
    """Reads <FIELD>_FILE environment variables and loads the secret from that file."""

    def __init__(self, settings_cls: type[BaseSettings], environ: dict[str, str]) -> None:
        super().__init__(settings_cls)
        self._environ = environ

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        path = self._environ.get(f"{field_name.upper()}_FILE")
        if not path:
            return None, field_name, False
        # strip(): files created by shell tools often end with a newline.
        return Path(path).read_text(encoding="utf-8").strip(), field_name, False

    def __call__(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for name in SECRET_FIELDS:
            value, key, _ = self.get_field_value(self.settings_cls.model_fields[name], name)
            if value is not None:
                data[key] = value
        return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    service_name: str = "api"
    env: str = "dev"
    log_level: str = "INFO"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "switchyard"
    postgres_db: str = "switchyard"
    postgres_password: SecretStr = SecretStr("")
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_connect_timeout: float = 3.0

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: SecretStr = SecretStr("")

    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "switchyard"
    rabbitmq_password: SecretStr = SecretStr("")

    # Outgoing HTTP client (providers, other processes): limits and timeouts are explicit.
    http_max_connections: int = 100
    http_max_keepalive: int = 20
    http_keepalive_expiry: float = 30.0
    http_connect_timeout: float = 3.0
    http_read_timeout: float = 60.0
    http_write_timeout: float = 10.0
    http_pool_timeout: float = 5.0

    # How long a readiness probe waits for each dependency.
    health_check_timeout: float = 2.0

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Order = priority: explicit arguments, then *_FILE secrets, then plain env.
        return (
            init_settings,
            _FileSecretsSource(settings_cls, dict(os.environ)),
            env_settings,
        )

    @property
    def database_url(self) -> str:
        # quote(): a password with special characters must not break the URL.
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        auth = f":{password}@" if password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/0"

    @property
    def amqp_url(self) -> str:
        password = quote(self.rabbitmq_password.get_secret_value(), safe="")
        return (
            f"amqp://{quote(self.rabbitmq_user, safe='')}:{password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/"
        )
