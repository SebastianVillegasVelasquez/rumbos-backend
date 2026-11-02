"""Covers as assets of their own: a track points at its cover, and the cover is
referenced by it for usage, deletion protection and garbage collection."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from app.enums import AssetKind
from app.exceptions import AssetInUseError
from app.models import Asset
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.schemas.asset import AssetCreate, AssetRecord
from app.scripts.gc_assets import find_garbage
from app.tenancy.scope import SYSTEM, bind_scope


def new_cover(**fields: object) -> AssetCreate:
    return AssetCreate.model_validate(
        {
            "id": uuid7(),
            "kind": AssetKind.COVER,
            "mime": "image/webp",
            "width": 512,
            "height": 512,
            "size_bytes": 2000,
            "sha256": uuid.uuid4().hex * 2,
            "title": "Portada",
            **fields,
        }
    )


def new_track(cover: uuid.UUID | None = None, **fields: object) -> AssetCreate:
    return AssetCreate.model_validate(
        {
            "id": uuid7(),
            "kind": AssetKind.AUDIO,
            "mime": "audio/mpeg",
            "size_bytes": 1000,
            "sha256": uuid.uuid4().hex * 2,
            "title": "Pista",
            "credit": "Autor",
            "duration_seconds": 12.5,
            "cover_asset_id": cover,
            **fields,
        }
    )


@pytest.fixture
def repo(session: AsyncSession) -> SqlAlchemyAssetRepository:
    return SqlAlchemyAssetRepository(session)


async def make(
    repo: SqlAlchemyAssetRepository, *, tracks: int = 1
) -> tuple[AssetRecord, list[AssetRecord]]:
    """A cover and `tracks` audio tracks that share it."""
    cover = await repo.create(new_cover())
    return cover, [await repo.create(new_track(cover.id)) for _ in range(tracks)]


async def test_a_track_remembers_its_cover(repo: SqlAlchemyAssetRepository) -> None:
    cover, [track] = await make(repo)

    assert track.cover_asset_id == cover.id
    stored = await repo.get_by_id(track.id)
    assert stored is not None and stored.cover_asset_id == cover.id
    assert cover.cover_asset_id is None


async def test_a_cover_counts_the_tracks_that_use_it(
    repo: SqlAlchemyAssetRepository,
) -> None:
    cover, _ = await make(repo, tracks=3)

    usage = (await repo.usage([cover.id]))[cover.id]

    assert (usage.tracks, usage.total) == (3, 3)
    assert (usage.maps, usage.skins, usage.publications) == (0, 0, 0)


async def test_a_cover_in_use_cannot_be_deleted(
    repo: SqlAlchemyAssetRepository,
) -> None:
    cover, _ = await make(repo)

    with pytest.raises(AssetInUseError) as caught:
        await repo.delete_if_unused(cover.id)

    assert caught.value.usage.tracks == 1
    assert await repo.get_by_id(cover.id) is not None


async def test_a_cover_can_be_deleted_once_its_track_lets_go(
    repo: SqlAlchemyAssetRepository,
) -> None:
    cover, [track] = await make(repo)

    result = await repo.set_cover(track.id, None)

    assert result is not None
    updated, previous = result
    assert updated.cover_asset_id is None and previous == cover.id
    assert await repo.delete_if_unused(cover.id) is True
    assert await repo.get_by_id(cover.id) is None


async def test_deleting_the_track_releases_the_cover(
    repo: SqlAlchemyAssetRepository,
) -> None:
    cover, [track] = await make(repo)

    assert await repo.delete_if_unused(track.id) is True

    assert await repo.delete_if_unused(cover.id) is True


async def test_a_shared_cover_stays_while_any_track_still_uses_it(
    repo: SqlAlchemyAssetRepository,
) -> None:
    cover, [first, second] = await make(repo, tracks=2)

    await repo.set_cover(first.id, None)
    with pytest.raises(AssetInUseError):
        await repo.delete_if_unused(cover.id)
    assert (await repo.usage([cover.id]))[cover.id].tracks == 1

    await repo.set_cover(second.id, None)
    assert await repo.delete_if_unused(cover.id) is True


async def test_set_cover_returns_the_cover_it_replaces(
    repo: SqlAlchemyAssetRepository,
) -> None:
    old, [track] = await make(repo)
    new = await repo.create(new_cover())

    result = await repo.set_cover(track.id, new.id)

    assert result is not None
    updated, previous = result
    assert (updated.cover_asset_id, previous) == (new.id, old.id)
    assert (await repo.usage([old.id]))[old.id].tracks == 0
    assert (await repo.usage([new.id]))[new.id].tracks == 1


async def test_only_an_audio_track_can_be_given_a_cover(
    repo: SqlAlchemyAssetRepository,
) -> None:
    cover, _ = await make(repo)

    assert await repo.set_cover(cover.id, cover.id) is None
    assert await repo.set_cover(uuid.uuid4(), cover.id) is None


async def test_the_database_refuses_what_is_not_a_track_pointing_at_a_cover(
    repo: SqlAlchemyAssetRepository, session: AsyncSession
) -> None:
    cover, [track] = await make(repo)

    # An image with a cover,
    with pytest.raises(IntegrityError, match="ck_assets_cover_link"):
        await repo.create(
            AssetCreate.model_validate(
                {
                    "id": uuid7(),
                    "kind": AssetKind.BUBBLE,
                    "mime": "image/png",
                    "width": 8,
                    "height": 8,
                    "size_bytes": 10,
                    "sha256": uuid.uuid4().hex * 2,
                    "title": "Imagen",
                    "cover_asset_id": cover.id,
                }
            )
        )
    # a track that is its own cover,
    with pytest.raises(IntegrityError, match="ck_assets_cover_link"):
        await session.execute(
            update(Asset).where(Asset.id == track.id).values(cover_asset_id=track.id)
        )
    await session.rollback()
    # and a cover that does not exist.
    with pytest.raises(IntegrityError):
        await repo.create(new_track(uuid.uuid4()))


async def test_covers_are_not_listed_unless_asked_for(
    repo: SqlAlchemyAssetRepository,
) -> None:
    cover, [track] = await make(repo)

    everything, total = await repo.list(None, None, 50, 0)
    covers, cover_total = await repo.list(AssetKind.COVER, None, 50, 0)

    assert [a.id for a in everything] == [track.id] and total == 1
    assert [a.id for a in covers] == [cover.id] and cover_total == 1


async def test_covers_count_in_the_tenants_quota(
    repo: SqlAlchemyAssetRepository,
) -> None:
    await make(repo)

    assert await repo.total_bytes() == 2000 + 1000


async def test_the_references_name_the_tracks_that_use_a_cover(
    repo: SqlAlchemyAssetRepository,
) -> None:
    cover, [track] = await make(repo)

    refs = (await repo.references([cover.id]))[cover.id]

    assert [(t.id, t.title) for t in refs.tracks] == [(track.id, "Pista")]


# --- serving -----------------------------------------------------------------


async def test_a_cover_is_in_service_while_a_live_track_uses_it(
    repo: SqlAlchemyAssetRepository, session: AsyncSession
) -> None:
    cover, [first, second] = await make(repo, tracks=2)
    lonely = await repo.create(new_cover())
    assert await repo.cover_in_service(cover.id) is True
    assert await repo.cover_in_service(lonely.id) is False  # nobody uses it

    now = datetime.now(UTC)
    await repo.set_quarantine(first.id, "complaint", now)
    assert await repo.cover_in_service(cover.id) is True  # the other still does

    await repo.set_quarantine(second.id, "complaint", now)
    assert await repo.cover_in_service(cover.id) is False

    await repo.set_quarantine(first.id, None, now)
    assert await repo.cover_in_service(cover.id) is True


# --- the library -------------------------------------------------------------


async def test_a_library_cover_has_no_tenant_and_every_tenant_can_read_it(
    engine: AsyncEngine, repo: SqlAlchemyAssetRepository
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as system:
        bind_scope(system, SYSTEM)
        library = SqlAlchemyAssetRepository(system)
        cover = await library.create(new_cover())
        track = await library.create(new_track(cover.id))
        spare = await library.create(new_cover())

    assert cover.is_library and track.is_library and spare.is_library
    seen = await repo.get_by_id(cover.id)  # the fixture's tenant
    assert seen is not None and seen.is_library
    # While the library's track uses it, it is in use for everybody,
    with pytest.raises(AssetInUseError):
        await repo.delete_if_unused(cover.id)
    # and even unused it is the operator's: a tenant cannot delete it.
    assert await repo.delete_if_unused(spare.id) is False
    assert await repo.get_by_id(spare.id) is not None


async def test_a_library_cover_is_not_garbage_however_old(
    engine: AsyncEngine,
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as system:
        bind_scope(system, SYSTEM)
        cover = await SqlAlchemyAssetRepository(system).create(new_cover())

        garbage = await find_garbage(
            system, timedelta(days=1), now=cover.created_at + timedelta(days=400)
        )

    assert cover.id not in {a.id for a in garbage}


# --- garbage collection ------------------------------------------------------


async def test_the_collector_keeps_covers_in_use_and_collects_forgotten_ones(
    engine: AsyncEngine, repo: SqlAlchemyAssetRepository
) -> None:
    used, _ = await make(repo)
    forgotten = await repo.create(new_cover())
    later = datetime.now(UTC) + timedelta(days=30)

    async with AsyncSession(engine, expire_on_commit=False) as system:
        bind_scope(system, SYSTEM)
        garbage = {a.id for a in await find_garbage(system, timedelta(days=7), later)}

    assert forgotten.id in garbage
    assert used.id not in garbage


async def test_collecting_a_track_frees_its_cover_for_the_next_run(
    engine: AsyncEngine, repo: SqlAlchemyAssetRepository
) -> None:
    cover, [track] = await make(repo)
    later = datetime.now(UTC) + timedelta(days=30)
    async with AsyncSession(engine, expire_on_commit=False) as system:
        bind_scope(system, SYSTEM)
        first = {a.id for a in await find_garbage(system, timedelta(days=7), later)}
        assert track.id in first and cover.id not in first  # still held by the track

        assert await SqlAlchemyAssetRepository(system).delete_if_unused(track.id)

        second = {a.id for a in await find_garbage(system, timedelta(days=7), later)}
    assert cover.id in second
