import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import KeyStatus, TenantStatus
from app.exceptions import (
    TenantIssuerTakenError,
    TenantKeyTakenError,
    TenantNotFoundError,
)
from app.models import Tenant, TenantKey
from app.models.tenant import UQ_TENANT_KEY_KID
from app.repositories.sqlalchemy._errors import (
    FOREIGN_KEY_VIOLATION,
    constraint_name,
    sqlstate,
)
from app.schemas.tenant import NewTenantKey, TenantKeyRecord, TenantRecord


class SqlAlchemyTenantRepository:
    """Implements `TenantRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, tenant_id: uuid.UUID) -> TenantRecord | None:
        tenant = await self._session.get(Tenant, tenant_id)
        return TenantRecord.model_validate(tenant) if tenant else None

    async def get_by_issuer(self, issuer: str) -> TenantRecord | None:
        tenant = await self._session.scalar(
            select(Tenant).where(Tenant.issuer == issuer)
        )
        return TenantRecord.model_validate(tenant) if tenant else None

    async def list_all(self) -> list[TenantRecord]:
        result = await self._session.scalars(
            select(Tenant).order_by(Tenant.created_at, Tenant.id)
        )
        return [TenantRecord.model_validate(t) for t in result]

    async def create(
        self,
        name: str,
        issuer: str,
        host_origin: str,
        asset_quota_bytes: int,
        key: NewTenantKey,
    ) -> TenantRecord:
        tenant = Tenant(
            name=name,
            issuer=issuer,
            host_origin=host_origin,
            asset_quota_bytes=asset_quota_bytes,
        )
        try:
            self._session.add(tenant)
            await self._session.flush()  # the key needs the tenant's id
            self._session.add(
                TenantKey(
                    tenant_id=tenant.id, kid=key.kid, public_key_pem=key.public_key_pem
                )
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            # The only unique column of `tenants` is the issuer.
            raise TenantIssuerTakenError(issuer) from exc
        except BaseException:
            await self._session.rollback()
            raise
        return TenantRecord.model_validate(tenant)

    async def set_status(
        self, tenant_id: uuid.UUID, status: TenantStatus
    ) -> TenantRecord | None:
        stmt = (
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(status=status)
            .returning(Tenant)
            .execution_options(populate_existing=True)
        )
        try:
            tenant = (await self._session.scalars(stmt)).one_or_none()
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return TenantRecord.model_validate(tenant) if tenant else None

    async def add_key(self, tenant_id: uuid.UUID, key: NewTenantKey) -> TenantKeyRecord:
        row = TenantKey(
            tenant_id=tenant_id, kid=key.kid, public_key_pem=key.public_key_pem
        )
        try:
            self._session.add(row)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if constraint_name(exc) == UQ_TENANT_KEY_KID:
                raise TenantKeyTakenError(key.kid) from exc
            if sqlstate(exc) == FOREIGN_KEY_VIOLATION:
                raise TenantNotFoundError(tenant_id) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return TenantKeyRecord.model_validate(row)

    async def revoke_key(
        self, tenant_id: uuid.UUID, kid: str
    ) -> TenantKeyRecord | None:
        try:
            row = await self._session.scalar(
                select(TenantKey)
                .where(TenantKey.tenant_id == tenant_id, TenantKey.kid == kid)
                .with_for_update()
            )
            if row is None:
                await self._session.rollback()
                return None
            if row.status != KeyStatus.REVOKED:
                row.status = KeyStatus.REVOKED
                row.revoked_at = datetime.now(UTC)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return TenantKeyRecord.model_validate(row)

    async def list_keys(self, tenant_id: uuid.UUID) -> list[TenantKeyRecord]:
        result = await self._session.scalars(
            select(TenantKey)
            .where(TenantKey.tenant_id == tenant_id)
            .order_by(TenantKey.created_at, TenantKey.id)
        )
        return [TenantKeyRecord.model_validate(k) for k in result]

    async def active_key(
        self, tenant_id: uuid.UUID, kid: str
    ) -> TenantKeyRecord | None:
        row = await self._session.scalar(
            select(TenantKey).where(
                TenantKey.tenant_id == tenant_id,
                TenantKey.kid == kid,
                TenantKey.status == KeyStatus.ACTIVE,
            )
        )
        return TenantKeyRecord.model_validate(row) if row else None

    async def active_host_origins(self) -> list[str]:
        result = await self._session.scalars(
            select(Tenant.host_origin)
            .where(Tenant.status == TenantStatus.ACTIVE)
            .distinct()
            .order_by(Tenant.host_origin)
        )
        return list(result)
