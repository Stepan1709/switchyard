import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

NIL_UUID = uuid.UUID(int=0)  # "no model" (a request that failed before one was chosen)
# Upper bounds (ms) of the latency histogram; everything above the last one is counted too.
LATENCY_BOUNDS_MS = (50, 100, 250, 500, 1000, 2500, 5000, 10000)
UTILIZATION_SOURCES = ("live", "calibration", "shadow", "all")


class UsageDaily(Base):
    """One row per day, project, model and task type. A summary of the traffic log that
    outlives it: the log's old months are dropped, the summary stays.

    Rows are written with an upsert from the log, so computing a day again changes nothing
    unless the log itself changed (see analytics/usage.py)."""

    __tablename__ = "usage_daily"
    __table_args__ = (
        PrimaryKeyConstraint("day", "project_id", "model_id", "task_type", name="pk_usage_daily"),
        Index("ix_usage_daily_project_day", "project_id", "day"),
    )

    day: Mapped[date] = mapped_column(Date)  # UTC day
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))  # NIL_UUID: none
    task_type: Mapped[str] = mapped_column(String(64))  # '' when the request had none
    requests: Mapped[int] = mapped_column(Integer)
    ok: Mapped[int] = mapped_column(Integer)
    errors: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger)
    completion_tokens: Mapped[int] = mapped_column(BigInteger)
    # What the requests cost at the prices of the moment (a server paid by the month: 0 here,
    # its share of the monthly cost is added when a report is made).
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(16, 8))
    # What the same tokens would have cost on the project's reference model.
    reference_cost_usd: Mapped[Decimal] = mapped_column(Numeric(16, 8))
    duration_sum_ms: Mapped[float] = mapped_column(Float)
    # cumulative counts: {"le_50": n, ..., "le_10000": n, "total": n}
    latency_hist: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UtilizationDaily(Base):
    """How long a model was busy on a day: the length of the UNION of the time intervals in
    which it was handling at least one request (parallel requests are not added up)."""

    __tablename__ = "utilization_daily"
    __table_args__ = (
        PrimaryKeyConstraint("day", "model_id", "source", name="pk_utilization_daily"),
        CheckConstraint("source in ('live', 'calibration', 'shadow', 'all')", name="source"),
        CheckConstraint("busy_seconds >= 0 and busy_seconds <= 86400", name="busy_range"),
    )

    day: Mapped[date] = mapped_column(Date)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    # live: clients' requests, calibration: replays, shadow: copies, all: the three together
    source: Mapped[str] = mapped_column(String(12))
    busy_seconds: Mapped[float] = mapped_column(Float)
    requests: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
