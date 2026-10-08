import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AssetKind
from app.exceptions import AssetAlreadyExistsError
from app.models import Asset
from app.models.asset import UQ_ASSET_CONTENT
from app.repositories.sqlalchemy._errors import constraint_name
from app.schemas.asset import AssetRecord


class SqlAlchemyAssetRepository:
    """Implements `AssetRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, asset_id: uuid.UUID) -> AssetRecord | None:
        asset = await self._session.get(Asset, asset_id)
        return AssetRecord.model_validate(asset) if asset else None

    async def get_many(
        self, asset_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, AssetRecord]:
        if not asset_ids:
            return {}
        result = await self._session.scalars(
            select(Asset).where(Asset.id.in_(asset_ids))
        )
        return {a.id: AssetRecord.model_validate(a) for a in result}

    async def find_by_hash(self, kind: AssetKind, sha256: str) -> AssetRecord | None:
        asset = await self._session.scalar(
            select(Asset).where(Asset.kind == kind, Asset.sha256 == sha256)
        )
        return AssetRecord.model_validate(asset) if asset else None

    async def total_bytes(self) -> int:
        total = await self._session.scalar(
            select(func.coalesce(func.sum(Asset.size_bytes), 0))
        )
        return int(total or 0)

    async def create(
        self,
        asset_id: uuid.UUID,
        kind: AssetKind,
        mime: str,
        width: int,
        height: int,
        size_bytes: int,
        sha256: str,
    ) -> AssetRecord:
        asset = Asset(
            id=asset_id,
            kind=kind,
            mime=mime,
            width=width,
            height=height,
            size_bytes=size_bytes,
            sha256=sha256,
        )
        self._session.add(asset)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if constraint_name(exc) == UQ_ASSET_CONTENT:
                raise AssetAlreadyExistsError(sha256) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return AssetRecord.model_validate(asset)
