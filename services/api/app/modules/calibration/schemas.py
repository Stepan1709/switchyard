import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Thresholds(BaseModel):
    """What a candidate must reach to be recommended (used by the recommendation stage)."""

    # all on the 0..1 scale of the evaluation
    min_pass_rate: float | None = Field(default=None, ge=0, le=1)
    min_quality: float | None = Field(default=None, ge=0, le=1)
    max_p95_ms: int | None = Field(default=None, gt=0)
    max_error_rate: float | None = Field(default=None, ge=0, le=1)


class RunCreate(BaseModel):
    task_type: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    candidate_model_ids: list[uuid.UUID] = Field(min_length=1)
    sample_size: int = Field(gt=0)
    strategy: Literal["random", "stratified"] = "random"
    source: Literal["traffic", "labeled", "both"] = "traffic"
    # request: single requests. session: whole agent conversations (every step), so that the
    # run can say how many conversations a model would have got right at every step.
    # `sample_size` then counts conversations. Only for recorded traffic.
    sample_unit: Literal["request", "session"] = "request"
    # Fixed seed = the same pool gives the same sample. Chosen at random when omitted (and
    # stored, so the run can still be reproduced).
    seed: int | None = Field(default=None, ge=0, lt=2**31)
    since: datetime | None = None
    until: datetime | None = None
    # Budget. A run stops (status budget_exceeded) as soon as the next call would not fit.
    max_requests: int | None = Field(default=None, gt=0)
    max_cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6)
    thresholds: Thresholds = Thresholds()
    # The judge that grades answers that differ from the truth. Optional: without it only the
    # deterministic checks (and equality with the reference) are used. It must be neither a
    # candidate nor the reference. `judge_pairwise` compares two answers in both orders.
    judge_model_id: uuid.UUID | None = None
    judge_pairwise: bool = False


class RunProgress(BaseModel):
    total_jobs: int
    finished: int  # results stored so far
    percent: float
    by_status: dict[str, int]
    calls: int  # provider calls made
    spent_usd: Decimal
    remaining_requests: int | None
    remaining_usd: Decimal | None


class EvaluationProgress(BaseModel):
    status: str  # pending (the run has not ended) | running | done
    total: int  # answers to evaluate
    done: int


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    task_type: str
    status: str
    status_reason: str | None
    strategy: str
    source: str
    sample_unit: str
    seed: int
    sample_size: int
    samples_taken: int
    reference_model_id: uuid.UUID | None
    candidate_model_ids: list[uuid.UUID]
    max_requests: int | None
    max_cost_usd: Decimal | None
    thresholds: dict[str, Any]
    judge_model_id: uuid.UUID | None
    judge_pairwise: bool
    created_at: datetime
    finished_at: datetime | None
    progress: RunProgress | None = None
    evaluation: EvaluationProgress | None = None


class ResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    model_id: uuid.UUID
    status: str
    http_status: int | None
    error_code: str | None
    finish_reason: str | None
    has_tool_calls: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    tokens_estimated: bool
    ttft_ms: float | None
    latency_ms: float | None
    price_input_per_1m: Decimal | None
    price_output_per_1m: Decimal | None
    cost_usd: Decimal | None
    attempts: int
    response_payload: dict[str, Any] | None


class ResultPage(BaseModel):
    items: list[ResultOut]
    next_cursor: str | None


class ImportReport(BaseModel):
    total: int  # lines read
    imported: int
    duplicates: int  # requests that were already in the set
    available: int  # examples of this task type after the import
