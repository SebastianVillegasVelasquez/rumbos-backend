"""`available`: whether the files of an asset are really in the storage, read
live, and what it must never touch (snapshots and hashes)."""

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assets.storage import AssetStorage, content_key, thumb_key
from app.models import Publication
from app.tenancy.scope import SYSTEM
from tests.asset_helpers import png_asset
from tests.sessions import new_session
from tests.test_map_audio_api import (
    draft,
    make_map,
    music,
    published,
    set_music,
    track,
)
from tests.test_publications_api import add_bubble, publish, state


async def listed(client: httpx.AsyncClient, asset_id: str) -> dict[str, object]:
    items = (await client.get("/assets")).json()["items"]
    return next(item for item in items if item["id"] == asset_id)


async def test_a_listed_asset_is_available_while_its_files_exist(
    client: httpx.AsyncClient,
) -> None:
    made = await png_asset(client, 1, "background")

    assert (await listed(client, made["id"]))["available"] is True


async def test_a_listed_asset_is_not_available_once_its_file_is_gone_and_is_again_when_restored(
    client: httpx.AsyncClient, storage: AssetStorage
) -> None:
    made = await png_asset(client, 1, "bubble")
    key = content_key(uuid.UUID(made["id"]))
    data = await storage.get(key)

    await storage.delete(key)
    assert (await listed(client, made["id"]))["available"] is False

    await storage.put(key, data)
    assert (await listed(client, made["id"]))["available"] is True


async def test_a_missing_thumbnail_makes_a_background_unavailable(
    client: httpx.AsyncClient, storage: AssetStorage
) -> None:
    made = await png_asset(client, 1, "background")

    await storage.delete(thumb_key(uuid.UUID(made["id"])))

    assert (await listed(client, made["id"]))["available"] is False


async def test_one_missing_file_only_affects_its_own_asset(
    client: httpx.AsyncClient, storage: AssetStorage
) -> None:
    first = await png_asset(client, 1, "bubble")
    second = await png_asset(client, 2, "bubble")

    await storage.delete(content_key(uuid.UUID(first["id"])))

    assert (await listed(client, first["id"]))["available"] is False
    assert (await listed(client, second["id"]))["available"] is True


async def test_the_music_of_a_draft_map_says_whether_its_file_is_there(
    client: httpx.AsyncClient, storage: AssetStorage
) -> None:
    mine = await track(client, title="Brisa", credit="Ana")
    created = await make_map(client)
    await set_music(client, created["id"], music(mine["id"]))
    assert (await draft(client, created["id"]))["audioInfo"]["available"] is True

    await storage.delete(content_key(uuid.UUID(mine["id"])))

    info = (await draft(client, created["id"]))["audioInfo"]
    assert info["available"] is False
    assert info["title"] == "Brisa"  # the rest of the description is untouched


async def test_the_music_of_a_published_map_says_whether_its_file_is_there(
    client: httpx.AsyncClient, storage: AssetStorage
) -> None:
    mine = await track(client, title="Brisa", credit="Ana")
    created = await make_map(client)
    await add_bubble(client, created["id"], 1)
    await set_music(client, created["id"], music(mine["id"]))
    await publish(client, created["id"])
    assert (await published(client, created["id"]))["audioInfo"]["available"] is True

    await storage.delete(content_key(uuid.UUID(mine["id"])))

    read = await published(client, created["id"])
    assert read["audioInfo"]["available"] is False
    assert read["settings"]["audio"] is not None  # still the map's music


async def test_the_flag_never_reaches_a_snapshot_or_changes_a_hash(
    client: httpx.AsyncClient, storage: AssetStorage, engine: AsyncEngine
) -> None:
    mine = await track(client, title="Brisa", credit="Ana")
    created = await make_map(client)
    await add_bubble(client, created["id"], 1)
    await set_music(client, created["id"], music(mine["id"]))
    await publish(client, created["id"])
    before = await state(client, created["id"])

    await storage.delete(content_key(uuid.UUID(mine["id"])))
    after = await state(client, created["id"])

    assert after["draftHash"] == before["draftHash"]
    assert after["status"] == before["status"]
    async with new_session(engine, SYSTEM) as session:
        snapshot = (await session.scalars(select(Publication))).one().snapshot
    assert snapshot["audioInfo"].keys() == {"title", "credit", "durationSeconds"}
    assert "available" not in str(snapshot)


@pytest.mark.parametrize("path", ["", "/thumb"])
async def test_serving_a_missing_file_is_a_404_that_names_the_asset(
    client: httpx.AsyncClient,
    storage: AssetStorage,
    caplog: pytest.LogCaptureFixture,
    path: str,
) -> None:
    made = await png_asset(client, 1, "background")
    asset_id = uuid.UUID(made["id"])
    await storage.delete(thumb_key(asset_id) if path else content_key(asset_id))

    with caplog.at_level("ERROR"):
        r = await client.get(f"{made['url'].removeprefix('/api/v1')}{path}")

    assert r.status_code == 404
    messages = [m.getMessage() for m in caplog.records if m.levelname == "ERROR"]
    assert len(messages) == 1
    assert str(asset_id) in messages[0]
    assert "missing from storage" in messages[0]
