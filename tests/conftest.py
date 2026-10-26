import asyncio
import dataclasses
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from app.api.routes.assets import get_asset_settings, get_asset_storage
from app.assets.fingerprint import StorageState
from app.assets.storage import AssetStorage, LocalDiskAssetStorage
from app.core.config import Settings, get_settings
from app.enums import TenantStatus
from app.main import app as fastapi_app
from app.models import Tenant
from app.models.base import BaseORM
from app.models.tenant import LEGACY_TENANT_ID, LEGACY_TENANT_ISSUER
from app.services.asset_service import AssetSettings
from app.tenancy.scope import Scope, bind_scope
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.tenant_helpers import make_client  # noqa: F401  (a fixture)


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


@pytest.fixture(autouse=True)
def no_storage_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lifespan run by a test must not fingerprint the developer's real
    `ASSETS_DIR` against the real database; `test_storage_fingerprint` calls the
    real check itself."""

    async def healthy(engine: AsyncEngine, settings: Settings) -> StorageState:
        return StorageState.OK

    monkeypatch.setattr("app.main.verify_storage", healthy)


@pytest.fixture(scope="session", autouse=True)
def test_database() -> str:
    asyncio.run(_ensure_test_database())
    return _test_database_url()


LEGACY_QUOTA_BYTES = 1_000_000_000


@pytest.fixture
async def engine(test_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database)
    async with engine.begin() as conn:
        await conn.run_sync(BaseORM.metadata.drop_all)
        await conn.run_sync(BaseORM.metadata.create_all)
        # What the migration creates: the tenant every standalone request acts as.
        now = datetime.now(UTC)
        await conn.execute(
            insert(Tenant).values(
                id=LEGACY_TENANT_ID,
                name="legacy",
                issuer=LEGACY_TENANT_ISSUER,
                host_origin=LEGACY_TENANT_ISSUER,
                status=TenantStatus.ACTIVE,
                asset_quota_bytes=LEGACY_QUOTA_BYTES,
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(BaseORM.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as s:
        bind_scope(s, Scope(LEGACY_TENANT_ID))  # the standalone deployment's data
        yield s


# --- API client with the asset settings and storage under test control --------

ENABLED = AssetSettings(
    uploads_enabled=True,
    max_total_bytes=1_000_000_000,
    max_background_bytes=200_000,
    max_bubble_bytes=50_000,
    max_background_side=2000,
    max_bubble_side=100,
    audio_uploads_enabled=True,
    max_audio_bytes=200_000,
    max_audio_seconds=600.0,
    max_image_bytes=200_000,
    max_image_side=2000,
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
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c
