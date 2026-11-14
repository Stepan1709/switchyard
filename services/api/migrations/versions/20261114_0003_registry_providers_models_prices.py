"""registry providers models prices

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14 23:46:16.401454
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("auth_type", sa.String(length=16), nullable=False),
        sa.Column("is_local", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("api_key_encrypted", sa.String(length=1024), nullable=True),
        sa.Column("api_key_mask", sa.String(length=16), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_ok", sa.Boolean(), nullable=True),
        sa.Column("last_check_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_check_error", sa.String(length=300), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("auth_type in ('bearer', 'none')", name=op.f("ck_providers_auth_type")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_providers_name")),
    )
    op.create_table(
        "models",
        sa.Column("provider_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("supports_tools", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("supports_json", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("supports_stream", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("cost_type", sa.String(length=16), nullable=False),
        sa.Column("idle_threshold_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cost_type in ('per_token', 'fixed_monthly')", name=op.f("ck_models_cost_type")
        ),
        sa.CheckConstraint("context_window > 0", name=op.f("ck_models_context_positive")),
        sa.CheckConstraint(
            "idle_threshold_pct is null or (idle_threshold_pct >= 0 and idle_threshold_pct <= 100)",
            name=op.f("ck_models_idle_range"),
        ),
        sa.CheckConstraint(
            "max_output_tokens is null or max_output_tokens > 0",
            name=op.f("ck_models_output_positive"),
        ),
        sa.CheckConstraint("tier between 1 and 5", name=op.f("ck_models_tier_range")),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_models_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_models")),
        sa.UniqueConstraint("provider_id", "name", name=op.f("uq_models_provider_id_name")),
    )
    op.create_index(op.f("ix_models_provider_id"), "models", ["provider_id"], unique=False)
    op.create_table(
        "model_prices",
        sa.Column("model_id", sa.UUID(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_per_1m", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("output_per_1m", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("monthly_cost_usd", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "(input_per_1m is not null and output_per_1m is not null and monthly_cost_usd is null) or (input_per_1m is null and output_per_1m is null and monthly_cost_usd is not null)",
            name=op.f("ck_model_prices_shape"),
        ),
        sa.CheckConstraint(
            "coalesce(input_per_1m, 0) >= 0 and coalesce(output_per_1m, 0) >= 0 and coalesce(monthly_cost_usd, 0) >= 0",
            name=op.f("ck_model_prices_non_negative"),
        ),
        sa.CheckConstraint(
            "valid_to is null or valid_to > valid_from", name=op.f("ck_model_prices_interval")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["admins.id"],
            name=op.f("fk_model_prices_created_by_id_admins"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_model_prices_model_id_models"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_prices")),
    )
    op.create_index(
        "ix_model_prices_model_id_valid_from",
        "model_prices",
        ["model_id", "valid_from"],
        unique=False,
    )
    op.create_index(
        "uq_model_prices_current",
        "model_prices",
        ["model_id"],
        unique=True,
        postgresql_where=sa.text("valid_to is null"),
    )
    op.add_column("projects", sa.Column("reference_model_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_projects_reference_model_id_models"),
        "projects",
        "models",
        ["reference_model_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_projects_reference_model_id_models"), "projects", type_="foreignkey"
    )
    op.drop_column("projects", "reference_model_id")
    op.drop_index(
        "uq_model_prices_current",
        table_name="model_prices",
        postgresql_where=sa.text("valid_to is null"),
    )
    op.drop_index("ix_model_prices_model_id_valid_from", table_name="model_prices")
    op.drop_table("model_prices")
    op.drop_index(op.f("ix_models_provider_id"), table_name="models")
    op.drop_table("models")
    op.drop_table("providers")
