import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKeyMixin


class Feedback(UUIDPrimaryKeyMixin, Base):
    """A thumbs-up or thumbs-down that an application sent for one of its requests.

    The rating belongs to the answer the USER SAW, so the model and the task type are copied
    from the request when the feedback is stored. A second rating of the same request replaces
    the first (people change their minds; a repeated call must not count twice)."""

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_feedback_request_id"),
        CheckConstraint("rating in (-1, 1)", name="rating"),
        Index("ix_feedback_project_task_model", "project_id", "task_type", "model_id"),
        Index("ix_feedback_project_started", "project_id", "request_started_at"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    request_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_type: Mapped[str | None] = mapped_column(String(64))
    rating: Mapped[int] = mapped_column(SmallInteger)  # +1 or -1
    comment: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
