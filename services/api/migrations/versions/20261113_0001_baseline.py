"""baseline: empty revision

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Intentionally empty: tables arrive together with the stages that need them.
    pass


def downgrade() -> None:
    pass
