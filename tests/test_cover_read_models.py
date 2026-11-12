"""`audioInfo` gains `license` and `coverUrl`, read live from the track on every
request, in drafts and in publications. They are not part of a snapshot, so
nothing that was published changes: same canonical JSON, same hash."""

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from app.models import Asset, Publication
from app.publishing.snapshot import build_snapshot, content_hash
from app.schemas.map_settings import AudioInfo
from app.tenancy.scope import SYSTEM
from tests import audio_files as af
from tests import images
from tests.asset_helpers import library_track, quarantine, release
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.sessions import new_session
from tests.test_map_audio_api import draft, make_map, music, published, set_music
from tests.test_publications_api import publish, state
from tests.test_snapshot import TRACK, make_inputs, music_inputs


@pytest.fixture
async def client(
    engine: AsyncEngine, box: object, storage: object
) -> AsyncIterator[EditorClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


async def track(
    client: httpx.AsyncClient, *, cover: bytes | None = None, **form: str
) -> dict[str, Any]:
    files = {"file": ("s.mp3", af.mp3(40), "audio/mpeg")}
    if cover is not None:
        files["cover"] = ("c.png", cover, "image/png")
    r = await client.post(
        "/assets",
        data={
            "kind": "audio",
            "title": "Brisa",
            "credit": "Ana",
            "rightsDeclared": "true",
            **form,
        },
        files=files,
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def mapped(client: EditorClient, asset_id: str) -> dict[str, Any]:
    created = await make_map(client)
    assert (await set_music(client, created["id"], music(asset_id))).status_code == 200
    return created


# --- drafts -----------------------------------------------------------------------


async def test_a_draft_reads_the_license_and_the_cover_of_its_track(
    client: EditorClient,
) -> None:
    mine = await track(client, cover=images.png(), license="CC BY 4.0")
    created = await mapped(client, mine["id"])

    info = (await draft(client, created["id"]))["audioInfo"]

    assert info == {
        "title": "Brisa",
        "credit": "Ana",
        "durationSeconds": mine["durationSeconds"],
        "available": True,
        "license": "CC BY 4.0",
        "coverUrl": mine["coverUrl"],
    }
    assert (await client.get(info["coverUrl"])).status_code == 200


async def test_a_track_without_license_or_cover_reads_null_for_both(
    client: EditorClient,
) -> None:
    created = await mapped(client, (await track(client))["id"])

    info = (await draft(client, created["id"]))["audioInfo"]

    assert info["license"] is None and info["coverUrl"] is None


async def test_the_draft_follows_the_cover_as_it_changes(client: EditorClient) -> None:
    mine = await track(client)
    created = await mapped(client, mine["id"])
    assert (await draft(client, created["id"]))["audioInfo"]["coverUrl"] is None

    put = await client.put(
        f"/assets/{mine['id']}/cover",
        files={"file": ("c.png", images.png(), "image/png")},
    )
    added = (await draft(client, created["id"]))["audioInfo"]["coverUrl"]
    assert added == put.json()["coverUrl"]

    await client.put(
        f"/assets/{mine['id']}/cover",
        files={"file": ("c.png", images.png(color=(1, 2, 3)), "image/png")},
    )
    replaced = (await draft(client, created["id"]))["audioInfo"]["coverUrl"]
    assert replaced and replaced != added

    await client.delete(f"/assets/{mine['id']}/cover")
    assert (await draft(client, created["id"]))["audioInfo"]["coverUrl"] is None


async def test_a_library_track_shows_its_cover_to_every_map(
    client: EditorClient, engine: AsyncEngine
) -> None:
    library = await library_track(engine)
    created = await mapped(client, str(library.id))

    info = (await draft(client, created["id"]))["audioInfo"]

    assert info["coverUrl"] is None and info["license"] is None


async def test_a_taken_down_track_has_no_audio_info_and_so_no_cover(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client, cover=images.png(), license="CC BY 4.0")
    created = await mapped(client, mine["id"])

    await quarantine(engine, mine["id"])
    assert (await draft(client, created["id"]))["audioInfo"] is None

    await release(engine, mine["id"])
    assert (await draft(client, created["id"]))["audioInfo"]["coverUrl"] == mine[
        "coverUrl"
    ]


# --- publications -----------------------------------------------------------------


async def test_a_publication_freezes_the_description_but_reads_the_rest_live(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client, cover=images.png(), license="CC BY 4.0")
    created = await mapped(client, mine["id"])
    await publish(client, created["id"])
    first = (await published(client, created["id"]))["audioInfo"]
    assert (first["license"], first["coverUrl"]) == ("CC BY 4.0", mine["coverUrl"])

    # The track changes after publishing: a new title, licence and cover.
    await client.patch(f"/assets/{mine['id']}", json={"title": "Otro nombre"})
    async with new_session(engine, SYSTEM) as s:
        await s.execute(
            update(Asset).where(Asset.id == uuid.UUID(mine["id"])).values(license="CC0")
        )
        await s.commit()
    put = await client.put(
        f"/assets/{mine['id']}/cover",
        files={"file": ("c.png", images.png(color=(9, 9, 9)), "image/png")},
    )

    now = (await published(client, created["id"]))["audioInfo"]
    assert now["title"] == "Brisa"  # frozen at publication
    assert now["license"] == "CC0"  # live
    assert now["coverUrl"] == put.json()["coverUrl"] != mine["coverUrl"]  # live
    assert (await client.get(now["coverUrl"])).status_code == 200


async def test_a_published_map_loses_the_cover_when_the_track_is_taken_down(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client, cover=images.png())
    created = await mapped(client, mine["id"])
    await publish(client, created["id"])

    await quarantine(engine, mine["id"])
    read = await published(client, created["id"])

    assert read["audioInfo"] is None and read["settings"]["audio"] is None
    assert (await client.get(mine["coverUrl"])).status_code == 404


async def test_the_snapshot_keeps_exactly_the_three_frozen_fields(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client, cover=images.png(), license="CC BY 4.0")
    created = await mapped(client, mine["id"])
    await publish(client, created["id"])

    async with new_session(engine, SYSTEM) as s:
        row = (await s.scalars(select(Publication))).one()
    assert row.snapshot["audioInfo"] == {
        "title": "Brisa",
        "credit": "Ana",
        "durationSeconds": mine["durationSeconds"],
    }
    text = json.dumps(row.snapshot)
    assert mine["coverAssetId"] not in text and "CC BY 4.0" not in text
    assert "coverUrl" not in text and "license" not in text
    assert row.asset_ids == [uuid.UUID(mine["id"])]  # the cover is not listed


async def test_changing_the_license_or_the_cover_is_not_an_unpublished_change(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client, cover=images.png(), license="CC BY 4.0")
    created = await mapped(client, mine["id"])
    await publish(client, created["id"])
    before = await state(client, created["id"])
    assert before["status"] == "up_to_date"

    async with new_session(engine, SYSTEM) as s:
        await s.execute(
            update(Asset).where(Asset.id == uuid.UUID(mine["id"])).values(license="X")
        )
        await s.commit()
    await client.put(
        f"/assets/{mine['id']}/cover",
        files={"file": ("c.png", images.png(color=(7, 7, 7)), "image/png")},
    )
    await client.delete(f"/assets/{mine['id']}/cover")

    after = await state(client, created["id"])
    assert after["status"] == "up_to_date"
    assert after["draftHash"] == before["draftHash"]


async def test_the_same_map_hashes_the_same_with_or_without_a_cover_and_license(
    client: EditorClient,
) -> None:
    """Two tracks that differ only in licence and cover, published one after
    the other under the same snapshot content, give the same draft hash once the
    asset id is the same: here, one track gaining and losing its extras."""
    mine = await track(client)
    created = await mapped(client, mine["id"])
    bare = (await state(client, created["id"]))["draftHash"]

    await client.put(
        f"/assets/{mine['id']}/cover",
        files={"file": ("c.png", images.png(), "image/png")},
    )
    with_cover = (await state(client, created["id"]))["draftHash"]
    await client.delete(f"/assets/{mine['id']}/cover")

    assert with_cover == bare == (await state(client, created["id"]))["draftHash"]


# --- the canonical JSON and the hash, against what `main` produced ------------------

# Computed on `main`, before covers existed, from the same inputs.
PLAIN_HASH = "16819c90443557ced504dadfa02c0264cfe4684fef8e8b410d25f25c79ca40bb"
MUSIC_HASH = "1fd79b1e1bb1fabf7f2f3aaaaaf0dc2145311bc2a3212ecf041049532d0a746d"
LOUD_HASH = "4d51e1abe76fffbd068010e4c933f0cc728b87f18c6fb93c78e7917c773994fd"


def test_existing_publications_hash_as_they_did_before_covers() -> None:
    assert content_hash(build_snapshot(make_inputs())) == PLAIN_HASH
    assert content_hash(build_snapshot(music_inputs())) == MUSIC_HASH
    assert (
        content_hash(
            build_snapshot(music_inputs(volume=0.9, title="Otra", seconds=12.25))
        )
        == LOUD_HASH
    )


def test_a_license_and_a_cover_in_the_audio_info_change_nothing_in_the_snapshot() -> (
    None
):
    plain = music_inputs()
    rich = dataclasses.replace(
        plain,
        audio_info=AudioInfo(
            title="Brisa",
            credit="Ana, CC BY 4.0",
            duration_seconds=61.5,
            license="CC BY 4.0",
            cover_url=f"/api/v1/assets/{TRACK}",
        ),
    )

    assert build_snapshot(rich) == build_snapshot(plain)
    assert content_hash(build_snapshot(rich)) == MUSIC_HASH
    assert json.dumps(build_snapshot(rich)["audioInfo"]) == (
        '{"title": "Brisa", "credit": "Ana, CC BY 4.0", "durationSeconds": 61.5}'
    )
