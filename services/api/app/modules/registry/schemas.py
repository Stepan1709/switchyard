import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

AuthType = Literal["bearer", "none"]
CostType = Literal["per_token", "fixed_monthly"]


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    auth_type: AuthType = "bearer"
    is_local: bool = False
    # SecretStr: never printed by repr or logs, never serialised back.
    api_key: SecretStr | None = Field(default=None, max_length=512)


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    auth_type: AuthType | None = None
    is_local: bool | None = None
    enabled: bool | None = None
    api_key: SecretStr | None = Field(default=None, max_length=512)
    clear_api_key: bool = False


class ProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_url: str
    auth_type: AuthType
    is_local: bool
    enabled: bool
    api_key_mask: str | None
    last_check_at: datetime | None
    last_check_ok: bool | None
    last_check_latency_ms: int | None
    last_check_error: str | None
    created_at: datetime


class ConnectionTestRequest(BaseModel):
    model: str | None = Field(default=None, max_length=200)


class CallResult(BaseModel):
    ok: bool
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    detail: str | None = None


class ConnectionTestResult(BaseModel):
    ok: bool
    models_endpoint: CallResult
    completion: CallResult | None = None
    model: str | None = None


class ModelCreate(BaseModel):
    provider_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    tier: int = Field(default=1, ge=1, le=5)
    context_window: int = Field(gt=0, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, gt=0, le=10_000_000)
    supports_tools: bool = False
    supports_json: bool = False
    supports_stream: bool = True
    cost_type: CostType = "per_token"
    idle_threshold_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)


class ModelUpdate(BaseModel):
    """Partial update. `provider_id`, `name` and `cost_type` are fixed after creation: changing
    them would silently re-interpret every stored price and recorded request."""

    tier: int | None = Field(default=None, ge=1, le=5)
    context_window: int | None = Field(default=None, gt=0, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, gt=0, le=10_000_000)
    supports_tools: bool | None = None
    supports_json: bool | None = None
    supports_stream: bool | None = None
    idle_threshold_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    enabled: bool | None = None


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider_id: uuid.UUID
    name: str
    tier: int
    context_window: int
    max_output_tokens: int | None
    supports_tools: bool
    supports_json: bool
    supports_stream: bool
    cost_type: CostType
    idle_threshold_pct: Decimal | None
    enabled: bool
    created_at: datetime


class PriceCreate(BaseModel):
    # None = effective immediately. A future time schedules the change.
    effective_from: datetime | None = None
    input_per_1m: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6)
    output_per_1m: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6)
    monthly_cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)


class PriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    model_id: uuid.UUID
    valid_from: datetime
    valid_to: datetime | None
    input_per_1m: Decimal | None
    output_per_1m: Decimal | None
    monthly_cost_usd: Decimal | None
