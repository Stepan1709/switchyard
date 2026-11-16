import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class UsageRow(BaseModel):
    day: date | None = None
    model_id: uuid.UUID | None = None
    model: str | None = None
    task_type: str | None = None
    requests: int
    ok: int
    errors: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    reference_cost_usd: Decimal
    avg_latency_ms: float | None


class UsageReport(BaseModel):
    group_by: Literal["day", "model", "task_type"]
    since: date
    until: date
    rows: list[UsageRow]


class Totals(BaseModel):
    requests: int = 0
    ok: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal = Decimal(0)  # paid per token, at the prices of the moment
    fixed_cost_usd: Decimal = Decimal(0)  # the project's share of servers paid by the month
    effective_cost_usd: Decimal = Decimal(0)  # the two together: what the traffic really cost
    reference_cost_usd: Decimal = Decimal(0)  # the same tokens on the reference model
    savings_usd: Decimal = Decimal(0)  # reference - effective (negative: it cost more)
    savings_pct: float | None = None


class ModelUsage(Totals):
    model_id: uuid.UUID | None
    name: str
    cost_type: str | None
    share: float  # of the project's requests in the month
    avg_latency_ms: float | None
    effective_cost_per_request_usd: Decimal | None


class TaskTypeUsage(Totals):
    task_type: str


class KeyBudget(BaseModel):
    key_id: uuid.UUID
    name: str
    monthly_budget_usd: Decimal | None
    spent_usd: Decimal  # what the traffic log says
    counter_usd: Decimal | None  # what the gateway's counter in Redis says
    remaining_usd: Decimal | None
    used_pct: float | None
    exhausted: bool


class Problem(BaseModel):
    kind: Literal["idle_server", "dislikes_rose", "budget_exhausted"]
    subject: str
    detail: str


class Overview(BaseModel):
    month: str
    totals: Totals
    models: list[ModelUsage]
    task_types: list[TaskTypeUsage]
    keys: list[KeyBudget]
    problems: list[Problem]


class SourceBusy(BaseModel):
    live: float = 0.0
    calibration: float = 0.0
    shadow: float = 0.0


class ServerUsage(BaseModel):
    model_id: uuid.UUID
    name: str
    monthly_cost_usd: Decimal | None
    window_seconds: float
    busy_seconds: float  # union of the three kinds of load
    utilization_pct: float
    busy_by_source_seconds: SourceBusy  # each kind on its own (they overlap, so no sum)
    idle_threshold_pct: float
    idle: bool
    idle_cost_usd: Decimal | None  # paid for, and nobody used it
    requests: int
    effective_cost_per_request_usd: Decimal | None


class ServersReport(BaseModel):
    month: str
    servers: list[ServerUsage]


class FeedbackGroup(BaseModel):
    model_id: uuid.UUID | None
    model: str | None
    task_type: str | None
    ratings: int
    dislikes: int
    dislike_rate: float
    dislike_rate_low: float
    dislike_rate_high: float


class FeedbackReport(BaseModel):
    since: datetime
    until: datetime
    ratings: int
    dislikes: int
    groups: list[FeedbackGroup]


class FeedbackItem(BaseModel):
    request_id: uuid.UUID
    model: str | None
    task_type: str | None
    rating: int
    comment: str | None
    updated_at: datetime


class FeedbackList(BaseModel):
    items: list[FeedbackItem]


class RuleHealth(BaseModel):
    task_type: str
    rule_version: int
    activated_at: datetime
    suggest_rollback: bool
    reason: str
    ratings_since: int
    dislikes_since: int
    dislike_rate_since: float | None
    dislike_rate_low: float | None
    ratings_before: int
    dislikes_before: int
    dislike_rate_before: float | None


class RecomputeRequest(BaseModel):
    since: date
    until: date = Field(default_factory=date.today)


class BudgetState(BaseModel):
    key_id: uuid.UUID
    month: str
    monthly_budget_usd: Decimal | None
    spent_usd: Decimal
    counter_usd: Decimal | None
    remaining_usd: Decimal | None
    exhausted: bool


class HumanVerdictIn(BaseModel):
    verdict: Literal["pass", "fail"]
    comment: str | None = Field(default=None, max_length=1000)


class HumanVerdictOut(BaseModel):
    result_id: uuid.UUID
    verdict: str
    comment: str | None
    aggregates_recomputed: bool


class AgreementOut(BaseModel):
    run_id: uuid.UUID
    compared: int  # answers that both the judge and a person rated
    agreed: int
    agreement_rate: float | None
    cohen_kappa: float | None
    judge_pass_human_pass: int
    judge_pass_human_fail: int
    judge_fail_human_pass: int
    judge_fail_human_fail: int
    note: str
