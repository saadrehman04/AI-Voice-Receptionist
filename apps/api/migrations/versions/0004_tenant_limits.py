"""Per-business call limits (Phase 9).

`calls_per_hour_limit` and `monthly_minutes_limit` override the platform defaults
(TENANT_CALLS_PER_HOUR, TENANT_MONTHLY_MINUTES) for one business. NULL means "use the default".
They are set by whoever runs the platform, not from the owner dashboard.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("calls_per_hour_limit", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("monthly_minutes_limit", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_tenants_limits_positive",
        "tenants",
        "(calls_per_hour_limit IS NULL OR calls_per_hour_limit > 0) AND "
        "(monthly_minutes_limit IS NULL OR monthly_minutes_limit > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_limits_positive", "tenants", type_="check")
    op.drop_column("tenants", "monthly_minutes_limit")
    op.drop_column("tenants", "calls_per_hour_limit")
