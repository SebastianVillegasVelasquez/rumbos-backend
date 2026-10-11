import asyncio
import dataclasses
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.api.routes.assets import get_asset_settings, get_asset_storage
from app.assets.storage import AssetStorage, LocalDiskAssetStorage
from app.core.config import get_settings
from app.main import app as fastapi_app
from app.models.base import BaseORM
from app.services.asset_service import AssetSettings
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient


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


# --- API client with the asset settings and storage under test control --------

ENABLED = AssetSettings(
    uploads_enabled=True,
    max_total_bytes=1_000_000_000,
    max_background_bytes=200_000,
    max_bubble_bytes=50_000,
    max_background_side=2000,
    max_bubble_side=100,
)


class Box:
    """The settings the overridden dependency returns; tests tweak `current`."""

    current = ENABLED

    def change(self, **fields: Any) -> None:
        self.current = dataclasses.replace(self.current, **fields)


@pytest.fixture
def box() -> Iterator[Box]:
    box = Box()
    fastapi_app.dependency_overrides[get_asset_settings] = lambda: box.current
    yield box
    fastapi_app.dependency_overrides.pop(get_asset_settings, None)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[AssetStorage]:
    storage = LocalDiskAssetStorage(tmp_path / "assets")
    fastapi_app.dependency_overrides[get_asset_storage] = lambda: storage
    yield storage
    fastapi_app.dependency_overrides.pop(get_asset_storage, None)


@pytest.fixture
async def client(
    engine: AsyncEngine, box: Box, storage: AssetStorage
) -> AsyncIterator[httpx.AsyncClient]:
    fastapi_app.state.db_engine = engine
    fastapi_app.state.moodle_client = (
        InMemoryMoodleClient()
    )  # never called by these tests
    transport = httpx.ASGITransport(app=fastapi_app)
    async with EditorClient(transport=transport, base_url="http://t") as c:
        yield c
