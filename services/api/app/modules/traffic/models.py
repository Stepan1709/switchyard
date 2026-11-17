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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKeyMixin

TASK_MODES = ("record", "shadow", "route")


class TrafficRequest(Base):
    """One request that went through the gateway (a traffic log row).

    The table is partitioned by month on `started_at`: old data is removed by dropping a
    whole partition (instant, no table bloat) instead of deleting rows. PostgreSQL requires the
    partition key in every unique key, so the primary key is (started_at, request_id); the
    event carries `started_at` itself, so a re-delivered event hits the same key and
    `INSERT ... ON CONFLICT DO NOTHING` drops the duplicate.
    """

    __tablename__ = "requests"
    __table_args__ = (
        PrimaryKeyConstraint("started_at", "request_id", name="pk_requests"),
        Index("ix_requests_request_id", "request_id"),
        {"postgresql_partition_by": "RANGE (started_at)"},
    )

    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_type: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    stream: Mapped[bool] = mapped_column(Boolean)
    # ok | error | rate_limited | client_disconnected | timeout | upstream_error
    status: Mapped[str] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    # Names are copied (not referenced): the registry can change, the log must stay readable.
    requested_model: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(200))
    model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str | None] = mapped_column(String(100))
    provider_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    ttft_ms: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    overhead_ms: Mapped[float | None] = mapped_column(Float)
    # none_as_null: "no body" is SQL NULL. Without it SQLAlchemy stores the JSON value `null`,
    # which `IS NOT NULL` accepts, so a request logged without its body looked replayable.
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    payload_skipped: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    payload_masked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # An agent's conversation: the client's x-session-id, or a key derived from the first
    # messages. `step` counts the assistant turns before this request (0 = the first step).
    session_id: Mapped[str | None] = mapped_column(String(128))
    step: Mapped[int | None] = mapped_column(SmallInteger)
    # What the request cost at the prices of the moment, and what the same tokens would have
    # cost on the project's reference model (null: the tokens are unknown, e.g. an error).
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    reference_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TaskType(UUIDPrimaryKeyMixin, Base):
    """A kind of requests of a project (classification, extraction ...), set by x-task-type."""

    __tablename__ = "task_types"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_task_types_project_name"),
        CheckConstraint("mode in ('record', 'shadow', 'route')", name="mode"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    # record: everything goes to the reference model and is logged.
    # shadow: the same, and a share of the traffic is also sent to candidates in the background.
    # route: the active rule decides which model answers.
    mode: Mapped[str] = mapped_column(String(16), default="record", server_default="record")
    # shadow mode: which models get a copy and what share of requests is copied (0..1)
    shadow_model_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    shadow_rate: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    auto_registered: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# newest first within a project, optionally within a task type: the admin list view
Index(
    "ix_requests_project_started",
    TrafficRequest.project_id,
    TrafficRequest.started_at.desc(),
    TrafficRequest.request_id.desc(),
)
Index(
    "ix_requests_project_task_started",
    TrafficRequest.project_id,
    TrafficRequest.task_type,
    TrafficRequest.started_at.desc(),
    TrafficRequest.request_id.desc(),
)
# the steps of one conversation, for calibration runs that sample whole sessions
Index(
    "ix_requests_project_session",
    TrafficRequest.project_id,
    TrafficRequest.session_id,
    TrafficRequest.step,
    postgresql_where=TrafficRequest.session_id.is_not(None),
)
