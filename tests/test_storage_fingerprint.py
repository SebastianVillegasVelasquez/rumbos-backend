"""The storage fingerprint: the database and the directory must be the pair that
was filled together, or the app says so loudly."""

import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from app.assets.fingerprint import (
    MARKER_NAME,
    STORAGE_ID_KEY,
    StorageDegradedError,
    StorageState,
    StorageUnwritableError,
    adopt_storage,
    check_storage,
    read_marker,
)
from app.assets.storage import LocalDiskAssetStorage, content_key
from app.core.config import get_settings
from app.enums import AssetKind
from app.main import verify_storage
from app.repositories.sqlalchemy.app_meta_repository import SqlAlchemyAppMetaRepository
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.schemas.asset import AssetCreate
from app.tenancy.scope import SYSTEM
from tests.sessions import new_session


class Env:
    """A database, a storage directory, and the repositories over them."""

    def __init__(self, session: AsyncSession, root: Path) -> None:
        self.assets = SqlAlchemyAssetRepository(session)
        self.meta = SqlAlchemyAppMetaRepository(session)
        self.root = root
        self.storage = LocalDiskAssetStorage(root)

    async def add_asset(self, *, with_file: bool) -> uuid.UUID:
        asset_id = uuid7()
        await self.assets.create(
            AssetCreate(
                id=asset_id,
                kind=AssetKind.AUDIO,  # the only kind the system scope may create
                mime="audio/mpeg",
                size_bytes=1,
                sha256=asset_id.hex * 2,
                title="x",
                credit="c",
                duration_seconds=1.0,
            )
        )
        if with_file:
            await self.storage.put(content_key(asset_id), b"x")
        return asset_id

    async def check(self, *, fail_fast: bool = False) -> StorageState:
        return (
            await check_storage(self.assets, self.meta, self.root, fail_fast=fail_fast)
        ).state


@pytest.fixture
async def env(engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[Env]:
    # The system scope, as the start-up check runs: every tenant's assets count.
    async with new_session(engine, SYSTEM, expire_on_commit=False) as session:
        yield Env(session, tmp_path / "assets")


async def test_first_run_creates_the_id_and_the_marker(env: Env) -> None:
    assert await env.check() is StorageState.OK

    stored = await env.meta.get(STORAGE_ID_KEY)
    assert stored is not None
    uuid.UUID(stored)
    assert read_marker(env.root) == stored


async def test_a_normal_restart_keeps_the_id(env: Env) -> None:
    await env.check()
    first = await env.meta.get(STORAGE_ID_KEY)
    await env.add_asset(with_file=True)

    assert await env.check() is StorageState.OK

    assert await env.meta.get(STORAGE_ID_KEY) == first
    assert read_marker(env.root) == first


async def test_assets_without_a_marker_are_degraded_and_logged_as_an_error(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    await env.check()
    await env.add_asset(with_file=True)
    (env.root / MARKER_NAME).unlink()  # the volume was replaced by an empty one

    with caplog.at_level(logging.ERROR):
        assert await env.check() is StorageState.DEGRADED

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "volume" in errors[0].getMessage()
    assert "do NOT upload" in errors[0].getMessage()
    assert read_marker(env.root) is None  # nothing was written over the evidence


async def test_assets_with_a_different_marker_are_degraded(env: Env) -> None:
    await env.check()
    await env.add_asset(with_file=True)
    (env.root / MARKER_NAME).write_text(f"{uuid.uuid4()}\n")

    assert await env.check() is StorageState.DEGRADED


async def test_without_assets_a_missing_or_foreign_marker_is_simply_replaced(
    env: Env,
) -> None:
    await env.check()
    stored = await env.meta.get(STORAGE_ID_KEY)
    (env.root / MARKER_NAME).write_text(f"{uuid.uuid4()}\n")

    assert await env.check() is StorageState.OK

    assert read_marker(env.root) == stored


async def test_a_database_that_predates_the_fingerprint_adopts_a_directory_with_its_files(
    env: Env,
) -> None:
    await env.add_asset(with_file=True)

    assert await env.check() is StorageState.OK

    assert await env.meta.get(STORAGE_ID_KEY) == read_marker(env.root)


async def test_a_database_that_predates_the_fingerprint_is_degraded_over_an_empty_directory(
    env: Env,
) -> None:
    await env.add_asset(with_file=False)

    assert await env.check() is StorageState.DEGRADED

    assert await env.meta.get(STORAGE_ID_KEY) is None  # nothing was decided


async def test_a_rebuilt_database_reunites_with_the_marker_already_in_the_directory(
    env: Env,
) -> None:
    env.root.mkdir(parents=True)
    marker = str(uuid.uuid4())
    (env.root / MARKER_NAME).write_text(marker)

    assert await env.check() is StorageState.OK

    assert await env.meta.get(STORAGE_ID_KEY) == marker


async def test_an_unwritable_directory_is_an_error_naming_it(
    env: Env, tmp_path: Path
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    env.root = blocker / "assets"

    with pytest.raises(StorageUnwritableError, match="a-file"):
        await env.check()


async def test_fail_fast_refuses_to_start_when_degraded(env: Env) -> None:
    await env.check()
    await env.add_asset(with_file=True)
    (env.root / MARKER_NAME).unlink()

    with pytest.raises(StorageDegradedError, match="volume"):
        await env.check(fail_fast=True)


async def test_fail_fast_does_not_object_to_a_healthy_storage(env: Env) -> None:
    assert await env.check(fail_fast=True) is StorageState.OK
    assert await env.check(fail_fast=True) is StorageState.OK


async def test_adopting_makes_a_degraded_storage_the_right_one(env: Env) -> None:
    await env.check()
    await env.add_asset(with_file=False)
    (env.root / MARKER_NAME).unlink()
    assert await env.check() is StorageState.DEGRADED

    storage_id = await adopt_storage(env.meta, env.root)

    assert read_marker(env.root) == storage_id
    assert await env.check() is StorageState.OK


async def test_verify_storage_reports_the_state_for_the_lifespan(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    settings = get_settings().model_copy(update={"assets_dir": str(tmp_path / "a")})
    assert await verify_storage(engine, settings) is StorageState.OK

    async with new_session(engine, SYSTEM, expire_on_commit=False) as session:
        await Env(session, tmp_path / "a").add_asset(with_file=True)
    (tmp_path / "a" / MARKER_NAME).unlink()

    assert await verify_storage(engine, settings) is StorageState.DEGRADED
    strict = settings.model_copy(update={"storage_fail_fast": True})
    with pytest.raises(StorageDegradedError):
        await verify_storage(engine, strict)


async def test_verify_storage_without_the_table_is_degraded_not_a_crash(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE app_meta")
    settings = get_settings().model_copy(update={"assets_dir": str(tmp_path / "a")})

    assert await verify_storage(engine, settings) is StorageState.DEGRADED
