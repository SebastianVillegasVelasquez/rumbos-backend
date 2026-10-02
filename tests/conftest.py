from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.models.base import BaseORM


class Widget(BaseORM):
    """Throwaway model, test-only: proves BaseORM / the UoW work against Postgres."""

    __tablename__ = "test_widget"

    name: Mapped[str] = mapped_column(default="")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS test_widget"))
        await conn.run_sync(Widget.__table__.create)  # type: ignore[attr-defined]
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE test_widget"))
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
