import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKeyMixin

RUN_STATUSES = ("running", "paused", "completed", "cancelled", "budget_exceeded")
# A run in one of these states accepts no more work.
FINISHED_STATUSES = ("completed", "cancelled", "budget_exceeded")
STRATEGIES = ("random", "stratified")
SOURCES = ("traffic", "labeled", "both")
RESULT_STATUSES = ("ok", "error", "context_exceeded", "refused")


class LabeledSample(UUIDPrimaryKeyMixin, Base):
    """A hand-labelled example imported from JSONL: a request and what a good answer looks like."""

    __tablename__ = "labeled_samples"
    __table_args__ = (
        # the same request is not imported twice into one task type
        UniqueConstraint(
            "project_id", "task_type", "content_hash", name="uq_labeled_samples_content"
        ),
        Index("ix_labeled_samples_project_task", "project_id", "task_type"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    task_type: Mapped[str] = mapped_column(String(64))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CalibrationRun(UUIDPrimaryKeyMixin, Base):
    """One calibration: a sample of recorded requests replayed on several candidate models."""

    __tablename__ = "calibration_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('running', 'paused', 'completed', 'cancelled', 'budget_exceeded')",
            name="status",
        ),
        CheckConstraint("strategy in ('random', 'stratified')", name="strategy"),
        CheckConstraint("source in ('traffic', 'labeled', 'both')", name="source"),
        CheckConstraint("sample_size > 0", name="sample_positive"),
        CheckConstraint(
            "evaluation_status in ('pending', 'running', 'done')", name="evaluation_status"
        ),
        Index("ix_calibration_runs_project_created", "project_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    task_type: Mapped[str] = mapped_column(String(64))
    # The reference whose recorded answers are the baseline (copied from the project at creation).
    reference_model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="SET NULL")
    )
    candidate_model_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    status: Mapped[str] = mapped_column(String(24), default="running")
    status_reason: Mapped[str | None] = mapped_column(String(300))
    strategy: Mapped[str] = mapped_column(String(16), default="random")
    source: Mapped[str] = mapped_column(String(16), default="traffic")
    seed: Mapped[int] = mapped_column(Integer)
    sample_size: Mapped[int] = mapped_column(Integer)  # requested
    samples_taken: Mapped[int] = mapped_column(Integer, default=0)  # what the pool could give
    total_jobs: Mapped[int] = mapped_column(Integer, default=0)  # samples_taken x candidates
    # Budget: replay calls and money. null = unlimited.
    max_requests: Mapped[int | None] = mapped_column(Integer)
    max_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    # Evaluation of the answers. The judge (optional) is a model that is neither a candidate nor
    # the reference. `pairwise` makes it compare two answers in both orders instead of grading
    # the candidate against the reference.
    judge_model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="SET NULL")
    )
    judge_pairwise: Mapped[bool] = mapped_column(default=False, server_default="false")
    # pending (the run has not ended) -> running (judge.execute jobs are out) -> done
    evaluation_status: Mapped[str] = mapped_column(String(16), default="pending")
    evaluation_total: Mapped[int] = mapped_column(Integer, default=0)  # answers to evaluate
    # Thresholds for the recommendation (stage 9): quality, latency, errors.
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunSample(Base):
    """A request chosen for a run, copied so the run stays reproducible and inspectable after the
    traffic partition it came from has been dropped."""

    __tablename__ = "run_samples"
    __table_args__ = (PrimaryKeyConstraint("run_id", "request_id", name="pk_run_samples"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calibration_runs.id", ondelete="CASCADE")
    )
    # For recorded traffic this is the request id of the log; for labelled examples the id of
    # the labelled sample.
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(16))  # traffic | labeled
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # The reference answer (recorded traffic); expected is the labelled answer (imports).
    reference_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expected: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reference_prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    reference_completion_tokens: Mapped[int | None] = mapped_column(Integer)


class RunResult(UUIDPrimaryKeyMixin, Base):
    """What one candidate answered to one sampled request."""

    __tablename__ = "run_results"
    __table_args__ = (
        # Idempotency: a repeated job can never add a second result for the same pair.
        UniqueConstraint("run_id", "request_id", "model_id", name="uq_run_results_pair"),
        CheckConstraint("status in ('ok', 'error', 'context_exceeded', 'refused')", name="status"),
        Index("ix_run_results_run_model", "run_id", "model_id"),
        Index(
            "ix_run_results_run_status",
            "run_id",
            "status",
            postgresql_where=text("status <> 'ok'"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calibration_runs.id", ondelete="CASCADE")
    )
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    finish_reason: Mapped[str | None] = mapped_column(String(32))
    has_tool_calls: Mapped[bool] = mapped_column(default=False, server_default="false")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    tokens_estimated: Mapped[bool] = mapped_column(default=False, server_default="false")
    ttft_ms: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    # The price that was valid at replay time, written down so later price changes do not
    # rewrite history. Null for a fixed-monthly server (marginal cost is about zero).
    price_input_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    price_output_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    attempts: Mapped[int] = mapped_column(SmallInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
