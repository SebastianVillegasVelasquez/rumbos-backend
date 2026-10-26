from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppMeta


class SqlAlchemyAppMetaRepository:
    """Implements `AppMetaRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> str | None:
        value = await self._session.scalar(
            select(AppMeta.value).where(AppMeta.key == key)
        )
        await self._session.rollback()  # reads only: leave the session clean
        return value

    async def set_if_absent(self, key: str, value: str) -> str:
        try:
            # Several workers start at once: the first insert wins, the rest
            # read what it wrote.
            await self._session.execute(
                insert(AppMeta)
                .values(key=key, value=value)
                .on_conflict_do_nothing(index_elements=[AppMeta.key])
            )
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        stored = await self.get(key)
        assert stored is not None  # inserted above, or already there
        return stored

    async def set(self, key: str, value: str) -> None:
        try:
            await self._session.execute(
                insert(AppMeta)
                .values(key=key, value=value)
                .on_conflict_do_update(
                    index_elements=[AppMeta.key], set_={"value": value}
                )
            )
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
