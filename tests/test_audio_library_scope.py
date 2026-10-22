"""The audio library belongs to the operator: every tenant reads it, nobody but
the operator's tools changes it, and it is nobody's quota."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from uuid_utils.compat import uuid7

from app.enums import AssetKind, Role
from app.models import Asset
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.schemas.asset import AssetCreate
from app.tenancy.scope import SYSTEM, Scope, ScopeMissingError, bind_scope
from tests.asset_helpers import library_track, png_asset
from tests.editor_client import EditorClient
from tests.tenant_helpers import insert_tenant, insert_token

MakeClient = Callable[[str | None], EditorClient]


def new_audio(sha256: str, title: str = "Pista") -> AssetCreate:
    return AssetCreate(
        id=uuid7(),
        kind=AssetKind.AUDIO,
        mime="audio/mpeg",
        size_bytes=10,
        sha256=sha256,
        title=title,
        credit="Autor",
        duration_seconds=1.0,
    )


async def ids(client: httpx.AsyncClient, query: str) -> list[str]:
    r = await client.get(f"/assets?{query}")
    assert r.status_code == 200, r.text
    return [item["id"] for item in r.json()["items"]]


async def test_every_tenant_lists_the_library_and_only_through_its_scope(
    engine: AsyncEngine, client: httpx.AsyncClient, make_client: MakeClient
) -> None:
    track = await library_track(engine)
    beta = await insert_tenant(engine, "beta")
    other = make_client(await insert_token(engine, beta, 7, Role.EDITOR))

    for editor in (client, other):
        assert await ids(editor, "kind=audio&scope=library") == [str(track.id)]
        assert await ids(editor, "kind=audio&scope=tenant") == []
        assert await ids(editor, "kind=audio") == []  # tenant is the default


async def test_the_library_listing_shows_the_contract_fields(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    track = await library_track(engine, title="Brisa", credit="Ana, CC BY 4.0")

    [item] = (await client.get("/assets?kind=audio&scope=library")).json()["items"]

    assert item["id"] == str(track.id)
    assert (item["kind"], item["mime"]) == ("audio", "audio/mpeg")
    assert (item["title"], item["credit"]) == ("Brisa", "Ana, CC BY 4.0")
    assert item["durationSeconds"] == 12.5
    assert item["url"] == f"/api/v1/assets/{track.id}"
    assert (item["width"], item["height"], item["thumbUrl"]) == (None, None, None)
    assert "uploadedBySub" not in item and "tenantId" not in item


async def test_images_never_appear_in_the_library_scope(
    client: httpx.AsyncClient,
) -> None:
    image = await png_asset(client, 1, "background")

    assert await ids(client, "scope=library") == []
    assert await ids(client, "scope=tenant") == [image["id"]]


async def test_editors_can_neither_change_nor_delete_a_library_track(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    track = await library_track(engine)

    patched = await client.patch(f"/assets/{track.id}", json={"title": "Mine"})
    deleted = await client.delete(f"/assets/{track.id}")

    for r in (patched, deleted):
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "asset_is_library"
    assert await ids(client, "scope=library") == [str(track.id)]


async def test_quarantined_tracks_are_not_listed(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    await library_track(engine, quarantined=True)

    assert await ids(client, "scope=library") == []


async def test_a_track_made_inside_a_tenant_is_that_tenants(
    engine: AsyncEngine,
) -> None:
    tenant = await insert_tenant(engine, "gamma")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        bind_scope(session, Scope(tenant))

        record = await SqlAlchemyAssetRepository(session).create(new_audio("e" * 64))

    assert record.tenant_id == tenant


async def test_only_audio_can_be_global(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        bind_scope(session, SYSTEM)
        session.add(
            Asset(
                id=uuid7(),
                kind=AssetKind.BUBBLE,
                mime="image/png",
                width=8,
                height=8,
                size_bytes=10,
                sha256="f" * 64,
                title="Imagen",
            )
        )
        with pytest.raises(ScopeMissingError):
            await session.commit()


async def test_library_tracks_are_outside_every_quota_and_the_collector(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    await library_track(engine)
    repo = SqlAlchemyAssetRepository(session)

    assert await repo.total_bytes() == 0
    assert await repo.list_created_before(datetime.now(UTC) + timedelta(days=1)) == []


async def test_the_same_bytes_in_the_library_are_stored_once(
    engine: AsyncEngine,
) -> None:
    from app.exceptions import AssetAlreadyExistsError

    await library_track(engine, sha256="a" * 64)

    with pytest.raises(AssetAlreadyExistsError):
        await library_track(engine, sha256="a" * 64)


async def test_a_tenants_own_copy_wins_over_the_librarys(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    sha = uuid.uuid4().hex * 2
    shared = await library_track(engine, sha256=sha)
    repo = SqlAlchemyAssetRepository(session)
    own = await repo.create(new_audio(sha, "Copia"))

    found = await repo.find_by_hash(AssetKind.AUDIO, sha)

    assert found is not None and found.id == own.id != shared.id


async def test_a_library_track_is_visible_but_not_writable_in_a_tenant_session(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    from app.schemas.asset import AssetUpdate

    track = await library_track(engine)
    repo = SqlAlchemyAssetRepository(session)

    assert await repo.get_by_id(track.id) is not None
    assert await repo.update_metadata(track.id, AssetUpdate(title="Mine")) is None
    assert await repo.delete_if_unused(track.id) is False
    assert await repo.get_by_id(track.id) is not None
