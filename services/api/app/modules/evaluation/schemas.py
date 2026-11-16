import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CheckKind = Literal["json_schema", "exact_match", "regex", "tool_call_match", "llm_judge"]


class CheckCreate(BaseModel):
    kind: CheckKind
    # What the check needs: json_schema {"schema": {...}}; exact_match {"ignore_case"};
    # regex {"pattern", "mode"}; tool_call_match {"ignore_args", "names_only"};
    # llm_judge {"min_score", "rubric"}.
    config: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    position: int = Field(default=0, ge=0, le=1000)


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_type: str
    kind: str
    config: dict[str, Any]
    required: bool
    position: int
    created_at: datetime


class AggregateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model_id: uuid.UUID
    task_type: str
    n_total: int
    n_ok: int
    n_error: int
    n_context_exceeded: int
    n_evaluated: int
    n_judge_failed: int
    quality_mean: float | None
    quality_p10: float | None
    pass_rate: float | None
    pass_rate_low: float | None
    pass_rate_high: float | None
    valid_rate: float | None
    prompt_tokens_avg: float | None
    completion_tokens_avg: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    error_rate: float | None
    cost_per_request_usd: Decimal | None
    judge_cost_usd: Decimal | None
    insufficient_data: bool
    basis_counts: dict[str, int]
    computed_at: datetime


class AggregatesOut(BaseModel):
    run_id: uuid.UUID
    evaluation_status: str
    min_samples: int
    items: list[AggregateOut]


class EvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    result_id: uuid.UUID
    model_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    status: str
    basis: str
    quality: float | None
    passed: bool | None
    equal_to_truth: bool
    checks: list[dict[str, Any]]
    valid_schema: bool | None
    judge_mode: str | None
    judge_score: int | None
    judge_verdict: str | None
    judge_rationale: str | None
    judge_error: str | None
    judge_calls: int
    judge_prompt_version: int
    judge_cost_usd: Decimal | None


class EvaluationPage(BaseModel):
    items: list[EvaluationOut]
    next_cursor: str | None
