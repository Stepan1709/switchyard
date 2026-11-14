import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, UUIDPrimaryKeyMixin

AUTH_TYPES = ("bearer", "none")
COST_TYPES = ("per_token", "fixed_monthly")


class Provider(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An OpenAI-compatible endpoint (a cloud API or a self-hosted server)."""

    __tablename__ = "providers"
    __table_args__ = (CheckConstraint("auth_type in ('bearer', 'none')", name="auth_type"),)

    name: Mapped[str] = mapped_column(String(100), unique=True)
    base_url: Mapped[str] = mapped_column(String(500))
    auth_type: Mapped[str] = mapped_column(String(16), default="bearer")
    # local = a server we run ourselves; only such providers may live in private networks.
    is_local: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # AES-GCM ciphertext (see core/crypto.py); the plaintext key is never stored or returned.
    api_key_encrypted: Mapped[str | None] = mapped_column(String(1024))
    api_key_mask: Mapped[str | None] = mapped_column(String(16))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_check_latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_check_error: Mapped[str | None] = mapped_column(String(300))


class Model(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A model of a provider with its limits, capabilities and cost kind."""

    __tablename__ = "models"
    __table_args__ = (
        UniqueConstraint("provider_id", "name", name="uq_models_provider_id_name"),
        CheckConstraint("tier between 1 and 5", name="tier_range"),
        CheckConstraint("context_window > 0", name="context_positive"),
        CheckConstraint(
            "max_output_tokens is null or max_output_tokens > 0", name="output_positive"
        ),
        CheckConstraint("cost_type in ('per_token', 'fixed_monthly')", name="cost_type"),
        CheckConstraint(
            "idle_threshold_pct is null or (idle_threshold_pct >= 0 and idle_threshold_pct <= 100)",
            name="idle_range",
        ),
    )

    # RESTRICT: a provider that still has models cannot be deleted by accident.
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("providers.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))  # the model id as the provider knows it
    tier: Mapped[int] = mapped_column(SmallInteger, default=1)  # 1 = smallest ... 5 = largest
    context_window: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    supports_json: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    supports_stream: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    cost_type: Mapped[str] = mapped_column(String(16), default="per_token")
    # Below this utilisation (percent of time busy) a self-hosted model is flagged as idle.
    idle_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class ModelPrice(UUIDPrimaryKeyMixin, Base):
    """A price version valid in [valid_from, valid_to). valid_to is null for the current one.

    Rows are never updated except to close the interval, so the history stays exact and a
    request made last month can be priced with last month's rates.
    """

    __tablename__ = "model_prices"
    __table_args__ = (
        CheckConstraint("valid_to is null or valid_to > valid_from", name="interval"),
        CheckConstraint(
            "(input_per_1m is not null and output_per_1m is not null and monthly_cost_usd is null)"
            " or (input_per_1m is null and output_per_1m is null and monthly_cost_usd is not null)",
            name="shape",
        ),
        CheckConstraint(
            "coalesce(input_per_1m, 0) >= 0 and coalesce(output_per_1m, 0) >= 0"
            " and coalesce(monthly_cost_usd, 0) >= 0",
            name="non_negative",
        ),
        # At most one open (current) version per model.
        Index(
            "uq_model_prices_current",
            "model_id",
            unique=True,
            postgresql_where=text("valid_to is null"),
        ),
        Index("ix_model_prices_model_id_valid_from", "model_id", "valid_from"),
    )

    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE")
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # per_token models: USD per one million tokens.
    input_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    output_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    # fixed_monthly models: what the server costs per month in USD.
    monthly_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
