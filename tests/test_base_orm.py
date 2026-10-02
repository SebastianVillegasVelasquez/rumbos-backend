import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.models.base import BaseORM


class Widget(BaseORM):
    """Throwaway model, test-only: proves BaseORM works against Postgres."""

    __tablename__ = "test_widget"

    name: Mapped[str] = mapped_column(default="")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Widget.__table__.create)  # type: ignore[attr-defined]
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE test_widget"))
        await engine.dispose()


async def test_id_is_fresh_uuid7_per_row(session: AsyncSession) -> None:
    a, b = Widget(), Widget()
    session.add_all([a, b])
    await session.commit()
    assert isinstance(a.id, uuid.UUID) and a.id.version == 7
    assert a.id != b.id


async def test_timestamps_are_aware_and_fresh_per_row(session: AsyncSession) -> None:
    a = Widget()
    session.add(a)
    await session.commit()
    b = Widget()
    session.add(b)
    await session.commit()
    assert a.created_at.tzinfo is not None
    assert a.created_at.utcoffset() is not None
    assert b.created_at > a.created_at  # not frozen at import time


async def test_updated_at_refreshes_on_update_only(session: AsyncSession) -> None:
    w = Widget()
    session.add(w)
    await session.commit()
    created, first_updated = w.created_at, w.updated_at
    w.name = "changed"
    await session.commit()
    assert w.updated_at > first_updated
    assert w.created_at == created


async def test_columns_are_timestamptz(session: AsyncSession) -> None:
    async with session.bind.connect() as conn:  # type: ignore[union-attr]
        cols = await conn.run_sync(
            lambda c: {
                col["name"]: col["type"]
                for col in inspect(c).get_columns("test_widget")
            }
        )
    for name in ("created_at", "updated_at"):
        col_type = cols[name]
        assert isinstance(col_type, TIMESTAMP)
        assert col_type.timezone is True
