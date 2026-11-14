"""Application settings.

Non-secret values come from environment variables. A secret NAME can be given either as
NAME (plain, for tests) or as NAME_FILE (path to a file, e.g. /run/secrets/name): the file
variant is what Docker Compose secrets use, so passwords never appear in `environment`.
"""

import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

SECRET_FIELDS = (
    "postgres_password",
    "redis_password",
    "rabbitmq_password",
    "jwt_secret",
    "key_pepper",
    "admin_password",
    "master_keys",
)


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

    # Authentication. jwt_secret signs access tokens; key_pepper is the server-side secret
    # mixed into API key hashes (a database leak alone is then not enough to check guesses).
    jwt_secret: SecretStr = SecretStr("")
    key_pepper: SecretStr = SecretStr("")
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 7 * 24 * 3600
    password_min_length: int = 12
    # Login throttling: failures within the window, counted per email and per client IP.
    login_max_failures_per_email: int = 5
    login_max_failures_per_ip: int = 30
    login_window_seconds: int = 900

    # Encryption keys for provider credentials: lines "key_id:base64(32 bytes)", the first line
    # encrypts, all lines can decrypt (rotation). Provided as a secret file.
    master_keys: SecretStr = SecretStr("")

    # Provider addresses. Hosts listed here may be private-network names (mock_llm, a local
    # Ollama or vLLM server) and only for providers explicitly marked as local.
    local_provider_hosts: Annotated[list[str], NoDecode] = ["mock_llm", "ollama"]
    provider_connect_timeout: float = 5.0
    provider_read_timeout: float = 30.0
    provider_max_response_bytes: int = 1_048_576

    # Messaging. Retry delays in milliseconds: the n-th failure waits delays[n-1] before the
    # message comes back; after the last one it is dead-lettered. Changing them changes the
    # TTL of existing retry queues, which the broker refuses: use new queue names for that.
    retry_delays_ms: Annotated[list[int], NoDecode] = [5_000, 30_000, 300_000]
    delivery_limit: int = 10  # broker-side cap on redeliveries of one message (crash loops)
    consumer_prefetch: int = 10
    outbox_poll_interval: float = 0.5
    outbox_batch_size: int = 100
    # An event nobody is subscribed to is retried this many times, then dropped (and logged).
    outbox_unroutable_attempts: int = 5
    worker_port: int = 8004
    worker_shutdown_timeout: float = 25.0
    processed_retention_days: int = 14  # how long the inbox remembers handled message ids

    # Idempotency-Key for administrator POSTs.
    idempotency_ttl_hours: int = 24
    idempotency_lock_seconds: int = 120  # an in-progress claim older than this may be taken over

    # Optional bootstrap of the first administrator (created only if no admin exists yet).
    admin_email: str = ""
    admin_password: SecretStr = SecretStr("")

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

    @field_validator("local_provider_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [h.strip().lower() for h in value.split(",") if h.strip()]
        return value

    @field_validator("retry_delays_ms", mode="before")
    @classmethod
    def _split_delays(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(v) for v in value.split(",") if v.strip()]
        return value

    def require_auth_secrets(self) -> None:
        """Fail fast at startup: weak or missing secrets must never reach production."""
        for name in ("jwt_secret", "key_pepper"):
            if len(getattr(self, name).get_secret_value()) < 32:
                raise ValueError(f"{name} must be at least 32 characters (set {name.upper()}_FILE)")
        # Parsing validates the format and the key length; the result is built again at startup.
        from app.core.crypto import KeyRing

        KeyRing.from_text(self.master_keys.get_secret_value())

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
