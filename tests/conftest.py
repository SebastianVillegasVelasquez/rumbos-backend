import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

import app.models  # noqa: F401  (registers the real tables on the metadata)
from app.core.config import get_settings
from app.models.base import BaseORM


class Widget(BaseORM):
    """Throwaway model, test-only: proves BaseORM / the UoW work against Postgres."""

    __tablename__ = "test_widget"

    name: Mapped[str] = mapped_column(default="")


def _test_database_url(suffix: str = "test") -> str:
    """Same server as the POSTGRES_* settings, but a dedicated `<name>_<suffix>` database.

    Tests create and drop tables freely, so they must never share a database
    with migrated development data.
    """
    url = make_url(get_settings().build_database_url)
    return url.set(database=f"{url.database}_{suffix}").render_as_string(
        hide_password=False
    )


async def _admin_engine(url: URL) -> AsyncEngine:
    return create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )


async def recreate_database(url_text: str) -> None:
    """Drop and recreate an (empty) database, for tests that run migrations."""
    url = make_url(url_text)
    admin = await _admin_engine(url)
    async with admin.connect() as conn:
        await conn.execute(
            text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        )
        await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()


async def _ensure_test_database() -> None:
    url = make_url(_test_database_url())
    admin = await _admin_engine(url)
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
