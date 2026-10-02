import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

import app.models  # noqa: F401  (registers the real tables on the metadata)
from app.core.config import get_settings
from app.models.base import BaseORM


class Widget(BaseORM):
    """Throwaway model, test-only: proves BaseORM / the UoW work against Postgres."""

    __tablename__ = "test_widget"

    name: Mapped[str] = mapped_column(default="")


def _test_database_url() -> str:
    """Same server as DATABASE_URL, but a dedicated `<name>_test` database.

    Tests create and drop tables freely, so they must never share a database
    with migrated development data.
    """
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(
        hide_password=False
    )


async def _ensure_test_database() -> None:
    url = make_url(_test_database_url())
    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def test_database() -> str:
    asyncio.run(_ensure_test_database())
    return _test_database_url()


@pytest.fixture
async def engine(test_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database)
    async with engine.begin() as conn:
        await conn.run_sync(BaseORM.metadata.drop_all)
        await conn.run_sync(BaseORM.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(BaseORM.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
