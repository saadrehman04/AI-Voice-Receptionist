"""Tenants: config in Postgres (validated `TenantConfig` as JSON), cached in memory.

YAML files in `tenants/` are seed data: at startup any seed tenant missing from the database is
inserted. After that the database is the source of truth, so owners can edit their settings
from the dashboard and new businesses can sign up through onboarding.

The in-memory cache is per process and refreshed on every write made through this store.
(With several api processes, a write in one is seen by the others after their restart; a
pub/sub invalidation is noted as future work in DECISIONS.)
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist_api.auth import hash_password
from receptionist_api.db.models import ServiceRow, Tenant, User
from receptionist_api.db.session import transaction
from receptionist_shared.tenant import TenantConfig, list_tenants


class TenantExistsError(ValueError):
    pass


class EmailTakenError(ValueError):
    pass


class TenantRegistry:
    """Slug -> config and slug -> row id, in memory."""

    def __init__(self, tenants: list[TenantConfig] | None = None) -> None:
        self._by_slug = {t.slug: t for t in tenants or []}
        self._ids: dict[str, uuid.UUID] = {}

    @classmethod
    def from_dir(cls, tenants_dir: Path) -> TenantRegistry:
        return cls(list_tenants(tenants_dir))

    def get(self, slug: str) -> TenantConfig | None:
        return self._by_slug.get(slug)

    def id_of(self, slug: str) -> uuid.UUID | None:
        return self._ids.get(slug)

    def all(self) -> list[TenantConfig]:
        return list(self._by_slug.values())

    def put(self, tenant: TenantConfig, tenant_id: uuid.UUID) -> None:
        self._by_slug[tenant.slug] = tenant
        self._ids[tenant.slug] = tenant_id


async def _sync_services(session: AsyncSession, tenant_id: uuid.UUID, tenant: TenantConfig) -> None:
    await session.execute(delete(ServiceRow).where(ServiceRow.tenant_id == tenant_id))
    session.add_all(
        ServiceRow(
            tenant_id=tenant_id,
            name=s.name,
            duration_min=s.duration_min,
            price=s.price,
            currency=tenant.currency,
        )
        for s in tenant.services
    )


class TenantStore:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], registry: TenantRegistry
    ) -> None:
        self._sessions = sessions
        self.registry = registry

    async def seed(self, tenants: list[TenantConfig]) -> None:
        """Insert seed tenants that aren't in the DB yet; never overwrite owner edits."""
        async with transaction(self._sessions) as session:
            for tenant in tenants:
                row = await session.scalar(select(Tenant).where(Tenant.slug == tenant.slug))
                if row is None:
                    row = Tenant(
                        slug=tenant.slug,
                        name=tenant.business_name,
                        config=tenant.model_dump(mode="json"),
                    )
                    session.add(row)
                    await session.flush()
                    await _sync_services(session, row.id, tenant)
                elif row.config is None:
                    row.config = tenant.model_dump(mode="json")
                    await _sync_services(session, row.id, tenant)

    async def load(self) -> None:
        async with self._sessions() as session:
            rows = await session.scalars(select(Tenant).where(Tenant.active.is_(True)))
            for row in rows:
                if row.config is not None:
                    self.registry.put(TenantConfig.model_validate(row.config), row.id)

    async def create(
        self, tenant: TenantConfig, *, owner_email: str, password: str
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """New business + its owner account, in one transaction."""
        email = owner_email.strip().lower()
        async with transaction(self._sessions) as session:
            if await session.scalar(select(Tenant.id).where(Tenant.slug == tenant.slug)):
                raise TenantExistsError(tenant.slug)
            if await session.scalar(select(User.id).where(User.email == email)):
                raise EmailTakenError(email)
            row = Tenant(
                slug=tenant.slug, name=tenant.business_name, config=tenant.model_dump(mode="json")
            )
            session.add(row)
            await session.flush()
            await _sync_services(session, row.id, tenant)
            user = User(tenant_id=row.id, email=email, password_hash=hash_password(password))
            session.add(user)
            await session.flush()
            tenant_id, user_id = row.id, user.id
        self.registry.put(tenant, tenant_id)
        return tenant_id, user_id

    async def update(self, tenant_id: uuid.UUID, tenant: TenantConfig) -> None:
        async with transaction(self._sessions) as session:
            row = await session.get(Tenant, tenant_id)
            if row is None or row.slug != tenant.slug:
                raise ValueError("slug cannot change")
            row.config = tenant.model_dump(mode="json")
            row.name = tenant.business_name
            await _sync_services(session, tenant_id, tenant)
        self.registry.put(tenant, tenant_id)

    async def add_owner(self, tenant_id: uuid.UUID, email: str, password: str) -> uuid.UUID:
        async with transaction(self._sessions) as session:
            email = email.strip().lower()
            if await session.scalar(select(User.id).where(User.email == email)):
                raise EmailTakenError(email)
            user = User(tenant_id=tenant_id, email=email, password_hash=hash_password(password))
            session.add(user)
            await session.flush()
            return user.id

    async def find_user(self, email: str) -> tuple[User, str] | None:
        """(user, tenant slug) for login, or None."""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(User, Tenant.slug)
                    .join(Tenant, Tenant.id == User.tenant_id)
                    .where(User.email == email.strip().lower(), Tenant.active.is_(True))
                )
            ).first()
            return (row[0], row[1]) if row else None
