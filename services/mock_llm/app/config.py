"""mock_llm settings (environment variables with the MOCK_ prefix)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_", extra="ignore")

    # If set, requests must carry `Authorization: Bearer <api_key>` (like a real provider).
    api_key: str = ""
    # Multiplies every simulated delay. 1.0 = realistic, 0 = instant (unit tests).
    time_scale: float = 1.0
    # How long a `x-mock-fail: timeout` request hangs before giving up.
    timeout_seconds: float = 120.0
