"""tenants.phone_number (an unused placeholder) -> whatsapp_phone_number_id

The column mirrors TenantConfig.whatsapp_phone_number_id so the database guarantees that one
WhatsApp number belongs to at most one business (an incoming call must map to one tenant).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_tenants_phone", "tenants", type_="unique")
    op.alter_column(
        "tenants",
        "phone_number",
        new_column_name="whatsapp_phone_number_id",
        type_=sa.String(32),
        existing_nullable=True,
    )
    op.execute(
        "UPDATE tenants SET whatsapp_phone_number_id = config->>'whatsapp_phone_number_id' "
        "WHERE config ? 'whatsapp_phone_number_id'"
    )
    op.create_unique_constraint(
        "uq_tenants_whatsapp_number", "tenants", ["whatsapp_phone_number_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_tenants_whatsapp_number", "tenants", type_="unique")
    op.alter_column(
        "tenants",
        "whatsapp_phone_number_id",
        new_column_name="phone_number",
        type_=sa.String(20),
        existing_nullable=True,
    )
    op.create_unique_constraint("uq_tenants_phone", "tenants", ["phone_number"])
