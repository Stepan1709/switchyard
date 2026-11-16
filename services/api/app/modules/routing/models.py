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
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKeyMixin

RULE_STATUSES = ("proposed", "active", "superseded", "rolled_back", "rejected")
MAX_CHAIN = 5


class RoutingRule(UUIDPrimaryKeyMixin, Base):
    """A version of the routing rule of one task type: the models that answer, in order.

    The first model answers; the next ones are the fallbacks tried when it fails before it has
    sent anything. A rule is only ever created in `proposed` and activated by a person.

    proposed -> active -> superseded (a newer version took over)
                       -> rolled_back (a person undid it; the previous version came back)
    proposed -> rejected
    """

    __tablename__ = "routing_rules"
    __table_args__ = (
        UniqueConstraint("project_id", "task_type", "version", name="uq_routing_rules_version"),
        CheckConstraint(
            "status in ('proposed', 'active', 'superseded', 'rolled_back', 'rejected')",
            name="status",
        ),
        CheckConstraint("cardinality(chain) between 1 and 5", name="chain_length"),
        # Exactly one active version per (project, task type), enforced by the database itself
        # so that two simultaneous activations cannot both win.
        Index(
            "uq_routing_rules_active",
            "project_id",
            "task_type",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_routing_rules_project_task", "project_id", "task_type", "version"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    task_type: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    # model ids in order of use: [primary, fallback 1, ...]
    chain: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calibration_runs.id", ondelete="SET NULL")
    )
    # What the recommendation saw when it proposed this version (kept for the explanation).
    basis: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    note: Mapped[str | None] = mapped_column(String(500))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    activated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL")
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShadowResult(UUIDPrimaryKeyMixin, Base):
    """What a candidate answered to a copy of a live request (shadow mode)."""

    __tablename__ = "shadow_results"
    __table_args__ = (
        # One copy per request and model: a repeated job cannot add a second one.
        UniqueConstraint("request_id", "model_id", name="uq_shadow_results_pair"),
        Index("ix_shadow_results_project_task", "project_id", "task_type", "created_at"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    task_type: Mapped[str | None] = mapped_column(String(64))
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(24))  # ok | error | context_exceeded | refused
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    finish_reason: Mapped[str | None] = mapped_column(String(32))
    has_tool_calls: Mapped[bool] = mapped_column(default=False, server_default="false")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    ttft_ms: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    price_input_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    price_output_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
