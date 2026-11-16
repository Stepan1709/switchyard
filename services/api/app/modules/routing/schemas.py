import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RuleStatus = Literal["proposed", "active", "superseded", "rolled_back", "rejected"]


class RuleCreate(BaseModel):
    """A rule written by a person: the models that answer, in order of use."""

    chain: list[uuid.UUID] = Field(min_length=1, max_length=5)
    note: str | None = Field(default=None, max_length=500)


class ActivateRequest(BaseModel):
    # The version that the person saw as active when they decided (null: none was). If the
    # active version is another one by now, nothing changes and the answer is 409: somebody
    # else acted in between and the decision has to be made again.
    expected_active_version: int | None = None


class ProposeRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    task_type: str
    version: int
    status: RuleStatus
    chain: list[uuid.UUID]
    chain_names: list[str] = []
    source_run_id: uuid.UUID | None
    basis: dict[str, Any]
    note: str | None
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None


class VerdictOut(BaseModel):
    model_id: uuid.UUID
    name: str
    eligible: bool
    reasons: list[str]
    fixed_cost: bool
    cost_per_request_usd: Decimal | None
    pass_rate: float | None
    pass_rate_low: float | None
    pass_rate_high: float | None
    quality_mean: float | None
    latency_p95_ms: float | None
    error_rate: float | None
    n_evaluated: int
    insufficient_data: bool


class CriteriaOut(BaseModel):
    min_pass_rate: float
    min_quality: float | None
    max_p95_ms: float | None
    max_error_rate: float


class RecommendationOut(BaseModel):
    run_id: uuid.UUID
    task_type: str
    criteria: CriteriaOut
    primary: uuid.UUID | None
    chain: list[uuid.UUID]
    chain_names: list[str]
    verdicts: list[VerdictOut]
    notes: list[str]
