"""Database tables (SQLAlchemy 2). Every row carries `tenant_id`.

The key safety net is on `bookings`: an exclusion constraint makes Postgres itself refuse two
confirmed bookings for one tenant whose guarded time ranges overlap. Redis holds and the
re-check before writing make a conflict unlikely; this constraint makes it impossible.

`guard` = [start - buffer/2, end + buffer/2). Two bookings conflict when their guards overlap,
which is exactly "less than `buffer` minutes apart".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, UUID, ExcludeConstraint
from sqlalchemy.dialects.postgresql.ranges import Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Booking(TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_bookings_tenant_code"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_bookings_tenant_idem"),
        CheckConstraint("status in ('confirmed', 'cancelled')", name="ck_bookings_status"),
        CheckConstraint("ends_at > starts_at", name="ck_bookings_order"),
        ExcludeConstraint(
            ("tenant_id", "="),
            ("guard", "&&"),
            where=text("status = 'confirmed'"),
            using="gist",
            name="ex_bookings_no_overlap",
        ),
        Index("ix_bookings_tenant_phone", "tenant_id", "customer_phone"),
        Index("ix_bookings_tenant_start", "tenant_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    call_id: Mapped[str | None] = mapped_column(String(64))
    service: Mapped[str] = mapped_column(String(200), nullable=False)
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    guard: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
    calendar_event_id: Mapped[str | None] = mapped_column(String(1024))
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)


class Message(TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("kind in ('message', 'handoff')", name="ck_messages_kind"),
        CheckConstraint("status in ('new', 'done')", name="ck_messages_status"),
        Index("ix_messages_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    caller_name: Mapped[str | None] = mapped_column(String(120))
    caller_phone: Mapped[str | None] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
