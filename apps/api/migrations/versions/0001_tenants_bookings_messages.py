"""tenants, bookings (with no-overlap exclusion constraint), messages

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    # btree_gist lets one exclusion constraint mix "=" on tenant_id with "&&" on a range.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(63), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        *_timestamps(),
    )

    op.create_table(
        "bookings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("call_id", sa.String(64)),
        sa.Column("service", sa.String(200), nullable=False),
        sa.Column("customer_name", sa.String(120), nullable=False),
        sa.Column("customer_phone", sa.String(20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guard", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="confirmed"),
        sa.Column("calendar_event_id", sa.String(1024)),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "code", name="uq_bookings_tenant_code"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_bookings_tenant_idem"),
        sa.CheckConstraint("status in ('confirmed', 'cancelled')", name="ck_bookings_status"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_bookings_order"),
    )
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlap "
        "EXCLUDE USING gist (tenant_id WITH =, guard WITH &&) WHERE (status = 'confirmed')"
    )
    op.create_index("ix_bookings_tenant_phone", "bookings", ["tenant_id", "customer_phone"])
    op.create_index("ix_bookings_tenant_start", "bookings", ["tenant_id", "starts_at"])

    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("call_id", sa.String(64)),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("caller_name", sa.String(120)),
        sa.Column("caller_phone", sa.String(20)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        *_timestamps(),
        sa.CheckConstraint("kind in ('message', 'handoff')", name="ck_messages_kind"),
        sa.CheckConstraint("status in ('new', 'done')", name="ck_messages_status"),
    )
    op.create_index("ix_messages_tenant_created", "messages", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("bookings")
    op.drop_table("tenants")
