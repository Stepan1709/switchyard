import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKeyMixin

CHECK_KINDS = (
    "json_schema",
    "exact_match",
    "regex",
    "tool_call_match",
    "tool_args_valid",
    "llm_judge",
)
JUDGE_MODES = ("grade", "pairwise")


class TaskCheck(UUIDPrimaryKeyMixin, Base):
    """One check that is applied to every answer of a task type."""

    __tablename__ = "task_checks"
    __table_args__ = (
        CheckConstraint(
            "kind in ('json_schema', 'exact_match', 'regex', 'tool_call_match',"
            " 'tool_args_valid', 'llm_judge')",
            name="kind",
        ),
        Index("ix_task_checks_project_task", "project_id", "task_type", "position"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    task_type: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # A required check that fails makes the answer "not passed"; an optional one only lowers
    # the quality number.
    required: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    position: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class JudgePrompt(UUIDPrimaryKeyMixin, Base):
    """A version of a judge prompt. Versions are never edited: a change is a new version, so an
    old evaluation can always be explained by the exact text that produced it."""

    __tablename__ = "judge_prompts"
    __table_args__ = (
        UniqueConstraint("kind", "version", name="uq_judge_prompts_kind_version"),
        CheckConstraint("kind in ('grade', 'pairwise')", name="kind"),
    )

    kind: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text)
    user_template: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Evaluation(UUIDPrimaryKeyMixin, Base):
    """The verdict on one replay result."""

    __tablename__ = "evaluations"
    __table_args__ = (
        # Idempotency: one verdict per result and judge prompt version. 0 = no judge was used.
        UniqueConstraint("result_id", "judge_prompt_version", name="uq_evaluations_result_prompt"),
        CheckConstraint("status in ('ok', 'judge_failed', 'unevaluated')", name="status"),
        CheckConstraint("basis in ('expected', 'reference', 'none')", name="basis"),
        Index("ix_evaluations_run", "run_id"),
    )

    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("run_results.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calibration_runs.id", ondelete="CASCADE")
    )
    judge_prompt_version: Mapped[int] = mapped_column(Integer, default=0)
    # ok: evaluated | judge_failed: the judge gave no usable verdict (NOT a zero score) |
    # unevaluated: there was nothing to compare with and no check could be applied
    status: Mapped[str] = mapped_column(String(16))
    # What the answer was compared with: the labelled example or the reference model's answer.
    basis: Mapped[str] = mapped_column(String(16))
    quality: Mapped[float | None] = mapped_column(Float)  # 0..1
    passed: Mapped[bool | None] = mapped_column(Boolean)
    equal_to_truth: Mapped[bool] = mapped_column(Boolean, default=False)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    valid_schema: Mapped[bool | None] = mapped_column(Boolean)  # null: no json_schema check
    # Are the arguments of the answer's tool calls valid for the tools offered in the request
    # (known tool, arguments match its schema)? null: the answer made no tool call.
    valid_args: Mapped[bool | None] = mapped_column(Boolean)
    judge_mode: Mapped[str | None] = mapped_column(String(16))
    judge_model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    judge_score: Mapped[int | None] = mapped_column(SmallInteger)
    judge_verdict: Mapped[str | None] = mapped_column(String(32))
    judge_rationale: Mapped[str | None] = mapped_column(Text)
    judge_error: Mapped[str | None] = mapped_column(String(300))
    judge_calls: Mapped[int] = mapped_column(SmallInteger, default=0)
    judge_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    judge_completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    judge_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HumanVerdict(UUIDPrimaryKeyMixin, Base):
    """A person's verdict on a result. It outranks the judge. Only stored for now: the screens
    and endpoints that collect these verdicts come with the admin interface."""

    __tablename__ = "human_verdicts"
    __table_args__ = (
        UniqueConstraint("result_id", "admin_id", name="uq_human_verdicts_result_admin"),
        CheckConstraint("verdict in ('pass', 'fail')", name="verdict"),
    )

    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("run_results.id", ondelete="CASCADE")
    )
    admin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="CASCADE")
    )
    verdict: Mapped[str] = mapped_column(String(8))
    comment: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RunAggregate(Base):
    """Numbers per (run x model): what the recommendation and the matrix are built from."""

    __tablename__ = "run_aggregates"
    __table_args__ = (PrimaryKeyConstraint("run_id", "model_id", name="pk_run_aggregates"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calibration_runs.id", ondelete="CASCADE")
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE")
    )
    task_type: Mapped[str] = mapped_column(String(64))
    n_total: Mapped[int] = mapped_column(Integer)  # results stored for this model
    n_ok: Mapped[int] = mapped_column(Integer)
    n_error: Mapped[int] = mapped_column(Integer)  # error + refused
    n_context_exceeded: Mapped[int] = mapped_column(Integer)
    n_evaluated: Mapped[int] = mapped_column(Integer)  # with a quality number
    n_judge_failed: Mapped[int] = mapped_column(Integer)
    quality_mean: Mapped[float | None] = mapped_column(Float)
    quality_p10: Mapped[float | None] = mapped_column(Float)  # the lowest decile
    pass_rate: Mapped[float | None] = mapped_column(Float)
    pass_rate_low: Mapped[float | None] = mapped_column(Float)  # Wilson interval
    pass_rate_high: Mapped[float | None] = mapped_column(Float)
    valid_rate: Mapped[float | None] = mapped_column(Float)  # share of schema-valid answers
    valid_args_rate: Mapped[float | None] = mapped_column(Float)  # tool calls with valid arguments
    # Whole conversations (sample_unit = session): the share in which EVERY step passed.
    n_sessions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    n_sessions_passed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    session_pass_rate: Mapped[float | None] = mapped_column(Float)
    session_pass_low: Mapped[float | None] = mapped_column(Float)
    session_pass_high: Mapped[float | None] = mapped_column(Float)
    prompt_tokens_avg: Mapped[float | None] = mapped_column(Float)
    completion_tokens_avg: Mapped[float | None] = mapped_column(Float)
    latency_p50_ms: Mapped[float | None] = mapped_column(Float)
    latency_p95_ms: Mapped[float | None] = mapped_column(Float)
    ttft_p50_ms: Mapped[float | None] = mapped_column(Float)
    ttft_p95_ms: Mapped[float | None] = mapped_column(Float)
    error_rate: Mapped[float | None] = mapped_column(Float)
    cost_per_request_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    judge_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    insufficient_data: Mapped[bool] = mapped_column(Boolean)
    basis_counts: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
