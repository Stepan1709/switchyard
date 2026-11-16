"""Settings of the web interface (environment variables; secrets also as NAME_FILE)."""

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

SECRET_FIELDS = ("redis_password",)


class _FileSecretsSource(PydanticBaseSettingsSource):
    """Reads <FIELD>_FILE variables (Docker secrets) so passwords stay out of `environment`."""

    def __init__(self, settings_cls: type[BaseSettings], environ: dict[str, str]) -> None:
        super().__init__(settings_cls)
        self._environ = environ

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        path = self._environ.get(f"{field_name.upper()}_FILE")
        if not path:
            return None, field_name, False
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

    service_name: str = "web"
    env: str = "dev"
    log_level: str = "INFO"

    api_url: str = "http://api:8001"
    api_timeout: float = 15.0
    api_max_connections: int = 50

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: SecretStr = SecretStr("")
    # The api keeps its data in database 0; sessions of the web interface live in 1.
    redis_db: int = 1

    session_cookie: str = "swy_session"
    # Idle lifetime of a session: every request prolongs it.
    session_ttl_seconds: int = 8 * 3600
    # A visitor who has not logged in yet only needs the token of the login form.
    anonymous_ttl_seconds: int = 900
    # Over http (local development) a Secure cookie would never come back.
    cookie_secure: bool = True
    # Refresh the access token this long before it expires.
    refresh_margin_seconds: int = 30

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, _FileSecretsSource(settings_cls, dict(os.environ)), env_settings)

    @property
    def redis_url(self) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        auth = f":{password}@" if password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"
