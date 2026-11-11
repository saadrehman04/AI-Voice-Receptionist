"""owners, tenant config + services, call records (calls, turns, tool_calls)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()


def _id() -> sa.Column[object]:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _tenant() -> sa.Column[object]:
    return sa.Column(
        "tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


def _created() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    # Tenant facts move into the database (validated TenantConfig as JSON) so owners can edit
    # them from the dashboard; YAML files become seed data.
    op.add_column("tenants", sa.Column("config", JSONB, nullable=True))
    op.add_column("tenants", sa.Column("phone_number", sa.String(20), nullable=True))
    op.add_column(
        "tenants", sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.create_unique_constraint("uq_tenants_phone", "tenants", ["phone_number"])

    op.create_table(
        "users",
        _id(),
        _tenant(),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="owner"),
        _created(),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("role in ('owner', 'staff')", name="ck_users_role"),
    )

    op.create_table(
        "services",
        _id(),
        _tenant(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        _created(),
        sa.UniqueConstraint("tenant_id", "name", name="uq_services_tenant_name"),
    )

    op.create_table(
        "calls",
        _id(),
        _tenant(),
        sa.Column("call_id", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False, server_default="webrtc"),
        sa.Column("caller_phone", sa.String(20)),
        sa.Column("language", sa.String(8)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("outcome", sa.String(16)),
        sa.Column("intent", sa.String(32)),
        sa.Column("sentiment", sa.String(16)),
        sa.Column("summary", sa.Text()),
        sa.Column("recording_path", sa.String(500)),
        sa.Column("summary_status", sa.String(16), nullable=False, server_default="pending"),
        _created(),
        sa.UniqueConstraint("tenant_id", "call_id", name="uq_calls_tenant_call"),
        sa.CheckConstraint("status in ('active', 'ended')", name="ck_calls_status"),
        sa.CheckConstraint(
            "outcome is null or outcome in ('booked', 'rescheduled', 'cancelled', 'message', "
            "'handoff', 'no_action')",
            name="ck_calls_outcome",
        ),
    )
    op.create_index("ix_calls_tenant_started", "calls", ["tenant_id", "started_at"])

    op.create_table(
        "turns",
        _id(),
        _tenant(),
        sa.Column("call_pk", UUID, sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("interrupted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("timings", JSONB, nullable=True),
        sa.Column("spoken_at", sa.DateTime(timezone=True), nullable=False),
        _created(),
        sa.UniqueConstraint("call_pk", "idx", name="uq_turns_call_idx"),
        sa.CheckConstraint("speaker in ('caller', 'agent')", name="ck_turns_speaker"),
    )
    op.create_index("ix_turns_tenant_call", "turns", ["tenant_id", "call_pk"])

    op.create_table(
        "tool_calls",
        _id(),
        _tenant(),
        sa.Column("call_pk", UUID, sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False),
        _created(),
    )
    op.create_index("ix_tool_calls_tenant_call", "tool_calls", ["tenant_id", "call_pk"])


def downgrade() -> None:
    op.drop_table("tool_calls")
    op.drop_table("turns")
    op.drop_table("calls")
    op.drop_table("services")
    op.drop_table("users")
    op.drop_constraint("uq_tenants_phone", "tenants")
    op.drop_column("tenants", "active")
    op.drop_column("tenants", "phone_number")
    op.drop_column("tenants", "config")
