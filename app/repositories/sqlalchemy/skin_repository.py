import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Skin
from app.schemas.skin import ImageSkin, ProceduralSkin, SkinRead, dump_config


def _to_read(skin: Skin) -> SkinRead:
    return SkinRead(
        id=skin.id,
        name=skin.name,
        builtin=skin.is_builtin,
        is_default=skin.is_default,
        config=skin.config,  # validated against the contract on the way out
        created_at=skin.created_at,
        updated_at=skin.updated_at,
    )


class SqlAlchemySkinRepository:
    """Implements `SkinRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, skin_id: uuid.UUID) -> SkinRead | None:
        skin = await self._session.get(Skin, skin_id)
        return _to_read(skin) if skin else None

    async def existing_ids(self, skin_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
        if not skin_ids:
            return set()
        result = await self._session.scalars(
            select(Skin.id).where(Skin.id.in_(skin_ids))
        )
        return set(result)

    async def create(self, name: str, config: ProceduralSkin | ImageSkin) -> SkinRead:
        skin = Skin(name=name, kind=config.kind, config=dump_config(config))
        self._session.add(skin)
        try:
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return _to_read(skin)

    async def update(
        self,
        skin_id: uuid.UUID,
        name: str | None,
        config: ProceduralSkin | ImageSkin | None,
    ) -> SkinRead | None:
        values: dict[str, Any] = {}
        if name is not None:
            values["name"] = name
        if config is not None:
            values["kind"] = config.kind
            values["config"] = dump_config(config)
        stmt = (
            update(Skin)
            .where(Skin.id == skin_id)
            .values(**values)
            .returning(Skin)
            .execution_options(populate_existing=True)
        )
        try:
            skin = (await self._session.scalars(stmt)).one_or_none()
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return _to_read(skin) if skin else None

    async def delete(self, skin_id: uuid.UUID) -> bool:
        # One DELETE; the foreign keys (SET NULL / CASCADE) do the rest.
        try:
            result = await self._session.execute(
                delete(Skin).where(Skin.id == skin_id).returning(Skin.id)
            )
            deleted = result.scalar_one_or_none() is not None
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return deleted

    async def list(self, limit: int) -> list[SkinRead]:
        result = await self._session.scalars(
            select(Skin)
            .order_by(Skin.is_builtin.desc(), Skin.created_at, Skin.id)
            .limit(limit)
        )
        return [_to_read(skin) for skin in result]
