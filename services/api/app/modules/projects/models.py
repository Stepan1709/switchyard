import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A client application. Owns API keys; visible only to its members."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL")
    )


class ProjectMember(Base):
    """Membership: an administrator sees and manages only the projects they belong to."""

    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    admin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Client API key `swy_<prefix>_<secret>`.

    Only the public prefix (used for lookup) and an HMAC of the secret are stored, so the
    key cannot be recovered from the database.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("rate_limit_rpm is null or rate_limit_rpm > 0", name="rate_limit_positive"),
        CheckConstraint(
            "monthly_budget_usd is null or monthly_budget_usd >= 0", name="budget_non_negative"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(8), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    # Settings are stored now and enforced by the gateway in later stages. The task type is a
    # plain string until the task type registry exists.
    default_task_type: Mapped[str | None] = mapped_column(String(64))
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer)
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL")
    )
