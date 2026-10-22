import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    SQLColumnExpression,
    String,
    Text,
    cast,
    delete,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.paths import ASSETS_PATH
from app.enums import AssetKind, AssetScope
from app.exceptions import AssetAlreadyExistsError, AssetInUseError
from app.models import Asset, AssetAuditEvent, CourseMap, Publication, Skin
from app.models.asset import UQ_ASSET_CONTENT, UQ_LIBRARY_CONTENT
from app.repositories.sqlalchemy._errors import constraint_name
from app.schemas.asset import (
    AssetAuditEntry,
    AssetCreate,
    AssetRecord,
    AssetUpdate,
    AssetUsage,
)
from app.tenancy.scope import TENANT_WIDE


def _id_text(asset_id: SQLColumnExpression[Any]) -> ColumnElement[str]:
    return cast(asset_id, String)


# One definition of "uses this asset" per kind of user, shared by the counts and
# the guarded delete (and so by the garbage collector). Each takes an SQL
# expression for the asset id, so it works for a literal and for a join.
def map_uses(asset_id: SQLColumnExpression[Any]) -> ColumnElement[bool]:
    """A draft map whose background is `{ASSETS_PATH}/{id}`."""
    return CourseMap.image_url == func.concat(f"{ASSETS_PATH}/", _id_text(asset_id))


def skin_uses(asset_id: SQLColumnExpression[Any]) -> ColumnElement[bool]:
    """A skin whose config mentions the id anywhere. `strpos`, not `LIKE`, so
    nothing in an id can act as a wildcard; the id is always a bound value."""
    return func.strpos(cast(Skin.config, Text), _id_text(asset_id)) > 0


def publication_uses(asset_id: SQLColumnExpression[Any]) -> ColumnElement[bool]:
    """A publication whose snapshot depends on the asset (`asset_ids @> {id}`,
    which the GIN index serves)."""
    return Publication.asset_ids.contains(pg_array([asset_id]))


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

    async def find_by_hash(
        self, kind: AssetKind, sha256: str, *, library: bool = False
    ) -> AssetRecord | None:
        # The caller's own copy first, then the library's: a tenant that sends
        # a track the library already has is given the library's.
        stmt = select(Asset).where(Asset.kind == kind, Asset.sha256 == sha256)
        if library:
            stmt = stmt.where(Asset.tenant_id.is_(None))
        asset = await self._session.scalar(
            stmt.order_by(Asset.tenant_id.is_(None), Asset.id).limit(1)
        )
        return AssetRecord.model_validate(asset) if asset else None

    async def total_bytes(self) -> int:
        # The library is the operator's, not part of any tenant's quota.
        total = await self._session.scalar(
            select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(
                Asset.tenant_id.is_not(None)
            )
        )
        return int(total or 0)

    async def create(self, data: AssetCreate) -> AssetRecord:
        asset = Asset(
            id=data.id,
            kind=data.kind,
            mime=data.mime,
            width=data.width,
            height=data.height,
            size_bytes=data.size_bytes,
            sha256=data.sha256,
            title=data.title,
            credit=data.credit,
            original_filename=data.original_filename,
            duration_seconds=data.duration_seconds,
            license=data.license,
            uploaded_by_sub=data.uploaded_by_sub,
            rights_declared_at=data.rights_declared_at,
        )
        self._session.add(asset)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if constraint_name(exc) in (UQ_ASSET_CONTENT, UQ_LIBRARY_CONTENT):
                raise AssetAlreadyExistsError(data.sha256) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return AssetRecord.model_validate(asset)

    async def list_created_before(self, cutoff: datetime) -> list[AssetRecord]:
        result = await self._session.scalars(
            select(Asset)
            # Library tracks are curated, not uploaded and forgotten: never
            # garbage, however long nobody has used them.
            .where(Asset.created_at < cutoff, Asset.tenant_id.is_not(None))
            .order_by(Asset.created_at, Asset.id)
        )
        return [AssetRecord.model_validate(a) for a in result]

    async def update_metadata(
        self, asset_id: uuid.UUID, data: AssetUpdate
    ) -> AssetRecord | None:
        stmt = (
            update(Asset)
            .where(Asset.id == asset_id)
            .values(**data.model_dump(exclude_unset=True))
            .returning(Asset)
            .execution_options(populate_existing=True)
        )
        try:
            asset = (await self._session.scalars(stmt)).one_or_none()
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return AssetRecord.model_validate(asset) if asset else None

    async def usage(
        self, asset_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, AssetUsage]:
        ids = list(asset_ids)
        counts = {i: AssetUsage() for i in ids}
        if not ids:
            return counts
        # One grouped query per kind of user, however many assets are asked for.
        for field, model, uses in (
            ("maps", CourseMap, map_uses),
            ("skins", Skin, skin_uses),
            ("publications", Publication, publication_uses),
        ):
            rows = await self._session.execute(
                select(Asset.id, func.count(model.id))
                .select_from(Asset)
                .join(model, uses(Asset.id))
                .where(Asset.id.in_(ids))
                .group_by(Asset.id)
                # A tenant's library is shared by its courses: count the maps
                # of every course, not only the caller's.
                .execution_options(**{TENANT_WIDE: True})
            )
            for asset_id, n in rows:
                setattr(counts[asset_id], field, n)
        await self._session.rollback()  # reads only: leave the session clean
        return counts

    async def delete_if_unused(self, asset_id: uuid.UUID) -> bool:
        stmt = (
            delete(Asset)
            .where(
                Asset.id == asset_id,
                ~exists().where(map_uses(Asset.id)),
                ~exists().where(skin_uses(Asset.id)),
                ~exists().where(publication_uses(Asset.id)),
            )
            .returning(Asset.id)
            .execution_options(synchronize_session=False, **{TENANT_WIDE: True})
        )
        try:
            deleted = (await self._session.execute(stmt)).scalar_one_or_none()
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        if deleted is not None:
            return True
        # Nothing deleted: it never existed, or something uses it, or it is a
        # library track, which can be seen but is not the caller's to delete.
        if await self.get_by_id(asset_id) is None:
            return False
        usage = (await self.usage([asset_id]))[asset_id]
        if usage.total == 0:
            return False
        raise AssetInUseError(usage)

    async def set_quarantine(
        self, asset_id: uuid.UUID, reason: str | None, now: datetime
    ) -> tuple[AssetRecord, bool] | None:
        try:
            asset = await self._session.get(Asset, asset_id)
            if asset is None:
                return None
            changed = (asset.quarantined_at is None) == (reason is not None)
            if changed:
                asset.quarantined_at = now if reason is not None else None
                asset.quarantine_reason = reason
                self._session.add(
                    AssetAuditEvent(
                        asset_id=asset_id,
                        action="quarantined" if reason is not None else "released",
                        reason=reason,
                    )
                )
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return AssetRecord.model_validate(asset), changed

    async def record_event(
        self, asset_id: uuid.UUID, action: str, reason: str | None
    ) -> None:
        try:
            self._session.add(
                AssetAuditEvent(asset_id=asset_id, action=action, reason=reason)
            )
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise

    async def audit_events(self, asset_id: uuid.UUID) -> list[AssetAuditEntry]:
        result = await self._session.scalars(
            select(AssetAuditEvent)
            .where(AssetAuditEvent.asset_id == asset_id)
            .order_by(AssetAuditEvent.created_at, AssetAuditEvent.id)
        )
        return [AssetAuditEntry.model_validate(e) for e in result]

    async def list_audio(self) -> list[AssetRecord]:
        result = await self._session.scalars(
            select(Asset)
            .where(Asset.kind == AssetKind.AUDIO)
            .order_by(Asset.created_at, Asset.id)
        )
        return [AssetRecord.model_validate(a) for a in result]

    async def list(
        self,
        kind: AssetKind | None,
        q: str | None,
        limit: int,
        offset: int,
        scope: AssetScope = AssetScope.TENANT,
    ) -> tuple[list[AssetRecord], int]:
        # A tenant session sees its own rows and the library's; `scope` picks.
        filters: list[ColumnElement[bool]] = [
            Asset.tenant_id.is_(None)
            if scope is AssetScope.LIBRARY
            else Asset.tenant_id.is_not(None),
            Asset.quarantined_at.is_(None),
        ]
        if kind is not None:
            filters.append(Asset.kind == kind)
        if q:
            # `autoescape` makes "%" and "_" in the search text literal.
            filters.append(
                or_(
                    Asset.title.icontains(q, autoescape=True),
                    Asset.credit.icontains(q, autoescape=True),
                )
            )
        total = await self._session.scalar(
            select(func.count()).select_from(Asset).where(*filters)
        )
        # `id` (UUIDv7) makes the order total when timestamps tie.
        result = await self._session.scalars(
            select(Asset)
            .where(*filters)
            .order_by(Asset.created_at.desc(), Asset.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [AssetRecord.model_validate(a) for a in result], total or 0
