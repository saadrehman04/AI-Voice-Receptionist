"""Tenant configs loaded from YAML at startup (the files are the source of truth for facts)."""

from __future__ import annotations

from pathlib import Path

from receptionist_shared.tenant import TenantConfig, list_tenants


class TenantRegistry:
    def __init__(self, tenants: list[TenantConfig]) -> None:
        self._by_slug = {t.slug: t for t in tenants}

    @classmethod
    def from_dir(cls, tenants_dir: Path) -> TenantRegistry:
        return cls(list_tenants(tenants_dir))

    def get(self, slug: str) -> TenantConfig | None:
        return self._by_slug.get(slug)

    def all(self) -> list[TenantConfig]:
        return list(self._by_slug.values())
