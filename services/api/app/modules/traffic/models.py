import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    payload_skipped: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    payload_masked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
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
    # record: everything goes to the reference model and is logged. shadow and route arrive in
    # later stages; the column is here so that the registry does not change shape then.
    mode: Mapped[str] = mapped_column(String(16), default="record", server_default="record")
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
