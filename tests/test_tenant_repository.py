"""Tenants and their keys against real Postgres."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import KeyStatus, TenantStatus
from app.exceptions import (
    TenantIssuerTakenError,
    TenantKeyTakenError,
    TenantNotFoundError,
)
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.schemas.tenant import NewTenantKey, TenantRecord

KEY = NewTenantKey(kid="k1", public_key_pem="pem-1")


@pytest.fixture
def tenants(session: AsyncSession) -> SqlAlchemyTenantRepository:
    return SqlAlchemyTenantRepository(session)


async def _create(
    tenants: SqlAlchemyTenantRepository,
    name: str,
    issuer: str,
    origin: str | None = None,
) -> TenantRecord:
    return await tenants.create(name, issuer, origin or issuer, 10, KEY)


async def test_create_stores_the_tenant_and_its_first_key(
    tenants: SqlAlchemyTenantRepository,
) -> None:
    tenant = await _create(tenants, "Uni", "https://m.example.com")
    assert tenant.status is TenantStatus.ACTIVE
    assert await tenants.get_by_issuer("https://m.example.com") == tenant
    assert await tenants.get_by_id(tenant.id) == tenant
    key = await tenants.active_key(tenant.id, "k1")
    assert key is not None
    assert key.public_key_pem == "pem-1"


async def test_the_issuer_is_unique_and_nothing_is_left_behind(
    tenants: SqlAlchemyTenantRepository,
) -> None:
    await _create(tenants, "A", "https://m.example.com")
    with pytest.raises(TenantIssuerTakenError):
        await _create(tenants, "B", "https://m.example.com", "https://x.example.com")
    names = [
        t.name for t in await tenants.list_all() if t.issuer.endswith("example.com")
    ]
    assert names == ["A"]


async def test_disabling_and_enabling(tenants: SqlAlchemyTenantRepository) -> None:
    tenant = await _create(tenants, "A", "https://a.example.com")
    disabled = await tenants.set_status(tenant.id, TenantStatus.DISABLED)
    assert disabled is not None
    assert disabled.status is TenantStatus.DISABLED
    assert await tenants.active_host_origins() == []
    enabled = await tenants.set_status(tenant.id, TenantStatus.ACTIVE)
    assert enabled is not None
    assert await tenants.active_host_origins() == ["https://a.example.com"]


async def test_a_missing_tenant_is_none(tenants: SqlAlchemyTenantRepository) -> None:
    assert await tenants.set_status(uuid.uuid4(), TenantStatus.DISABLED) is None
    with pytest.raises(TenantNotFoundError):
        await tenants.add_key(uuid.uuid4(), KEY)


async def test_keys_are_added_revoked_and_never_reused(
    tenants: SqlAlchemyTenantRepository,
) -> None:
    tenant = await _create(tenants, "A", "https://a.example.com")
    await tenants.add_key(tenant.id, NewTenantKey(kid="k2", public_key_pem="pem-2"))
    with pytest.raises(TenantKeyTakenError):
        await tenants.add_key(tenant.id, NewTenantKey(kid="k2", public_key_pem="other"))

    revoked = await tenants.revoke_key(tenant.id, "k1")
    assert revoked is not None
    assert revoked.status is KeyStatus.REVOKED
    assert revoked.revoked_at is not None
    again = await tenants.revoke_key(tenant.id, "k1")
    assert again is not None
    assert again.revoked_at == revoked.revoked_at
    assert await tenants.active_key(tenant.id, "k1") is None
    assert await tenants.active_key(tenant.id, "k2") is not None
    with pytest.raises(TenantKeyTakenError):  # a revoked kid stays taken
        await tenants.add_key(tenant.id, KEY)
    assert await tenants.revoke_key(tenant.id, "nope") is None
    assert [k.kid for k in await tenants.list_keys(tenant.id)] == ["k1", "k2"]


async def test_host_origins_are_distinct_and_sorted(
    tenants: SqlAlchemyTenantRepository,
) -> None:
    await _create(tenants, "B", "https://b.example.com", "https://shared.example.com")
    await _create(tenants, "A", "https://a.example.com", "https://shared.example.com")
    await _create(tenants, "C", "https://c.example.com")
    assert await tenants.active_host_origins() == [
        "https://c.example.com",
        "https://shared.example.com",
    ]
