import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import TenantStatus
from app.exceptions import LaunchReplayedError
from app.models import AccessToken, Tenant, UsedJti
from app.repositories.sqlalchemy._errors import constraint_name
from app.schemas.auth import NewAccessToken, TokenGrant


class SqlAlchemyAuthRepository:
    """Implements `AuthRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register_launch(
        self,
        tenant_id: uuid.UUID,
        jti: str,
        jti_expires_at: datetime,
        token: NewAccessToken,
    ) -> None:
        try:
            # The primary key (tenant_id, jti) is what makes redeeming atomic:
            # of two simultaneous uses of one assertion exactly one inserts.
            self._session.add(
                UsedJti(tenant_id=tenant_id, jti=jti, expires_at=jti_expires_at)
            )
            self._session.add(AccessToken(**token.model_dump()))
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if constraint_name(exc) == "used_jtis_pkey":
                raise LaunchReplayedError(jti) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise

    async def find_grant(self, token_hash: str, now: datetime) -> TokenGrant | None:
        row = (
            await self._session.execute(
                select(AccessToken)
                .join(Tenant, Tenant.id == AccessToken.tenant_id)
                .where(
                    AccessToken.token_hash == token_hash,
                    AccessToken.expires_at > now,
                    Tenant.status == TenantStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()
        grant = TokenGrant.model_validate(row) if row else None
        await self._session.rollback()  # reads only: leave the session clean
        return grant

    async def purge_expired(self, now: datetime) -> tuple[int, int]:
        try:
            tokens = await self._session.execute(
                delete(AccessToken).where(AccessToken.expires_at <= now)
            )
            jtis = await self._session.execute(
                delete(UsedJti).where(UsedJti.expires_at <= now)
            )
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return tokens.rowcount, jtis.rowcount  # type: ignore[attr-defined]
