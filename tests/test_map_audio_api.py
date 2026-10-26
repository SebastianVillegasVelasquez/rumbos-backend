"""Per-map music through the real stack: choosing a track, reading it, using it
as an asset, publishing it, and what a takedown does to all of that."""

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.enums import Role
from app.main import app
from app.models import Asset, Publication
from app.models.tenant import LEGACY_TENANT_ID
from app.scripts.gc_assets import find_garbage
from app.tenancy.scope import SYSTEM
from tests import audio_files as af
from tests.asset_helpers import (
    library_track,
    png_asset,
    quarantine,
    release,
    upload_audio,
)
from tests.conftest import Box
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.sessions import new_session
from tests.tenant_helpers import insert_tenant, insert_token
from tests.test_publications_api import add_bubble, publish, state

MakeClient = Callable[[str | None], EditorClient]


@pytest.fixture
async def client(
    engine: AsyncEngine, box: Box, storage: object
) -> AsyncIterator[EditorClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


async def make_map(client: httpx.AsyncClient, course: int = 1) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={"title": "Mapa", "moodleCourseId": course, "imageUrl": "/m.png"},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def track(client: httpx.AsyncClient, n: int = 0, **form: str) -> dict[str, Any]:
    r = await upload_audio(client, af.mp3(40 + n), **form)
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def music(asset_id: str, **fields: Any) -> dict[str, Any]:
    return {"assetId": asset_id, **fields}


async def set_music(
    client: httpx.AsyncClient, map_id: str, audio: dict[str, Any] | None
) -> httpx.Response:
    return await client.put(
        f"/course-maps/{map_id}/appearance",
        json={"settings": {"audio": audio}, "defaultSkinId": None, "skinRules": []},
    )


async def draft(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(f"/course-maps/{map_id}")).json()
    return body


async def published(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    r = await client.get(f"/published/course-maps/{map_id}")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def with_music(
    client: httpx.AsyncClient, asset_id: str, **fields: Any
) -> dict[str, Any]:
    created = await make_map(client)
    r = await set_music(client, created["id"], music(asset_id, **fields))
    assert r.status_code == 200, r.text
    return created


# --- choosing and reading --------------------------------------------------------


async def test_a_new_map_has_no_music(client: EditorClient) -> None:
    created = await make_map(client)

    assert created["settings"]["audio"] is None
    assert created["audioInfo"] is None
    assert (await draft(client, created["id"]))["audioInfo"] is None


async def test_music_is_chosen_through_the_appearance_with_its_defaults(
    client: EditorClient,
) -> None:
    mine = await track(client, title="Brisa", credit="Ana, CC BY 4.0")
    created = await make_map(client)

    r = await set_music(client, created["id"], music(mine["id"]))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["settings"]["audio"] == {
        "assetId": mine["id"],
        "volume": 0.4,
        "loop": True,
        "startAfterInteraction": False,
    }
    assert body["audioInfo"] == {
        "title": "Brisa",
        "credit": "Ana, CC BY 4.0",
        "durationSeconds": mine["durationSeconds"],
        "available": True,
    }
    assert (await draft(client, created["id"]))["audioInfo"] == body["audioInfo"]


async def test_every_music_setting_is_stored(client: EditorClient) -> None:
    mine = await track(client)

    created = await with_music(
        client, mine["id"], volume=0.1, loop=False, startAfterInteraction=True
    )

    assert (await draft(client, created["id"]))["settings"]["audio"] == {
        "assetId": mine["id"],
        "volume": 0.1,
        "loop": False,
        "startAfterInteraction": True,
    }


async def test_the_info_of_a_draft_follows_the_track_live(
    client: EditorClient,
) -> None:
    mine = await track(client, title="Antes")
    created = await with_music(client, mine["id"])

    await client.patch(
        f"/assets/{mine['id']}", json={"title": "Después", "credit": "Otra"}
    )

    info = (await draft(client, created["id"]))["audioInfo"]
    assert (info["title"], info["credit"]) == ("Después", "Otra")


async def test_music_is_cleared_with_null(client: EditorClient) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])

    r = await set_music(client, created["id"], None)

    assert r.status_code == 200
    assert r.json()["settings"]["audio"] is None and r.json()["audioInfo"] is None


async def test_music_can_be_a_library_track_in_any_tenant(
    engine: AsyncEngine, client: EditorClient, make_client: MakeClient
) -> None:
    shared = await library_track(engine, title="Brisa", credit="Ana")
    other = make_client(
        await insert_token(engine, await insert_tenant(engine, "beta"), 3, Role.EDITOR)
    )

    for editor, course in ((client, 1), (other, 3)):
        created = await make_map(editor, course)
        r = await set_music(editor, created["id"], music(str(shared.id)))
        assert r.status_code == 200, r.text
        assert r.json()["audioInfo"]["title"] == "Brisa"


async def test_choosing_a_track_does_not_depend_on_the_upload_switch(
    client: EditorClient, box: Box
) -> None:
    mine = await track(client)
    box.change(audio_uploads_enabled=False)
    created = await make_map(client)

    assert (
        await set_music(client, created["id"], music(mine["id"]))
    ).status_code == 200


# --- what cannot be chosen ---------------------------------------------------------


async def test_an_unknown_track_is_422(client: EditorClient) -> None:
    created = await make_map(client)
    missing = str(uuid.uuid4())

    r = await set_music(client, created["id"], music(missing))

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "audio_asset_not_found"
    assert r.json()["detail"]["assetId"] == missing


async def test_an_image_is_not_music(client: EditorClient) -> None:
    image = await png_asset(client, 1, "background")
    created = await make_map(client)

    r = await set_music(client, created["id"], music(image["id"]))

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "audio_asset_wrong_kind"


async def test_a_quarantined_track_cannot_be_chosen(
    engine: AsyncEngine, client: EditorClient
) -> None:
    mine = await track(client)
    await quarantine(engine, mine["id"])
    created = await make_map(client)

    r = await set_music(client, created["id"], music(mine["id"]))

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "audio_asset_not_found"


async def test_another_tenants_track_does_not_exist_for_you(
    engine: AsyncEngine, client: EditorClient, make_client: MakeClient
) -> None:
    theirs = make_client(
        await insert_token(engine, await insert_tenant(engine, "beta"), 3, Role.EDITOR)
    )
    their_track = await track(theirs)
    created = await make_map(client)

    r = await set_music(client, created["id"], music(their_track["id"]))

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "audio_asset_not_found"


@pytest.mark.parametrize(
    "audio",
    [
        {"volume": 0.5},  # no asset
        {"assetId": "not-a-uuid"},
        {"assetId": str(uuid.uuid4()), "volume": -0.1},
        {"assetId": str(uuid.uuid4()), "volume": 1.1},
        {"assetId": str(uuid.uuid4()), "volume": "loud"},
        {"assetId": str(uuid.uuid4()), "autoplay": True},
    ],
)
async def test_malformed_music_settings_are_422(
    client: EditorClient, audio: dict[str, Any]
) -> None:
    created = await make_map(client)

    assert (await set_music(client, created["id"], audio)).status_code == 422


async def test_volume_zero_and_one_are_valid(client: EditorClient) -> None:
    mine = await track(client)
    created = await make_map(client)

    for volume in (0, 1, 0.0, 1.0):
        r = await set_music(client, created["id"], music(mine["id"], volume=volume))
        assert r.status_code == 200, r.text


async def test_a_rejected_appearance_changes_nothing(client: EditorClient) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"], volume=0.9)
    before = await draft(client, created["id"])

    r = await client.put(
        f"/course-maps/{created['id']}/appearance",
        json={
            "settings": {"mode": "guided", "audio": music(str(uuid.uuid4()))},
            "defaultSkinId": None,
            "skinRules": [],
        },
    )

    assert r.status_code == 422
    assert await draft(client, created["id"]) == before


# --- the track as an asset ----------------------------------------------------------


async def test_a_track_in_use_is_counted_and_cannot_be_deleted(
    client: EditorClient,
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])

    listed = (await client.get("/assets?kind=audio")).json()["items"]
    refused = await client.delete(f"/assets/{mine['id']}")

    assert listed[0]["usage"] == {"maps": 1, "skins": 0, "publications": 0}
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "asset_in_use"
    assert refused.json()["detail"]["usage"]["maps"] == 1

    await set_music(client, created["id"], None)
    assert (await client.delete(f"/assets/{mine['id']}")).status_code == 204


async def test_a_track_used_by_a_published_map_cannot_be_deleted(
    client: EditorClient,
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])
    await publish(client, created["id"])
    await set_music(client, created["id"], None)  # the draft lets go of it

    refused = await client.delete(f"/assets/{mine['id']}")

    assert refused.status_code == 409
    assert refused.json()["detail"]["usage"] == {
        "maps": 0,
        "skins": 0,
        "publications": 1,
    }


async def test_the_collector_keeps_tracks_that_maps_or_publications_use(
    client: EditorClient, engine: AsyncEngine
) -> None:
    used = await track(client, 1)
    published_only = await track(client, 2)
    unused = await track(client, 3)
    first = await with_music(client, used["id"])
    second = await with_music(client, published_only["id"])
    await publish(client, second["id"])
    await set_music(client, second["id"], None)
    del first

    async with new_session(engine, SYSTEM) as session:
        garbage = await find_garbage(session, timedelta(0))

    assert [a.id for a in garbage] == [uuid.UUID(unused["id"])]


async def test_a_track_is_in_use_for_every_course_of_the_tenant(
    engine: AsyncEngine, client: EditorClient, make_client: MakeClient
) -> None:
    mine = await track(client)
    await with_music(client, mine["id"])
    elsewhere = make_client(
        await insert_token(engine, LEGACY_TENANT_ID, 99, Role.EDITOR)
    )

    refused = await elsewhere.delete(f"/assets/{mine['id']}")

    assert refused.status_code == 409


# --- publishing ----------------------------------------------------------------------


async def test_a_map_published_without_music_stores_neither_key(
    client: EditorClient, engine: AsyncEngine
) -> None:
    created = await make_map(client)
    await add_bubble(client, created["id"], 1)

    await publish(client, created["id"])

    async with new_session(engine, SYSTEM) as s:
        snapshot = (await s.scalars(select(Publication.snapshot))).one()
    assert "audioInfo" not in snapshot and "audio" not in snapshot["settings"]
    read = await published(client, created["id"])
    assert read["audioInfo"] is None and read["settings"]["audio"] is None


async def test_publishing_music_freezes_it_and_lists_the_asset(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client, title="Brisa", credit="Ana")
    created = await with_music(client, mine["id"], volume=0.7)

    await publish(client, created["id"])

    async with new_session(engine, SYSTEM) as s:
        row = (await s.scalars(select(Publication))).one()
    assert row.asset_ids == [uuid.UUID(mine["id"])]
    assert row.snapshot["settings"]["audio"]["assetId"] == mine["id"]
    assert row.snapshot["audioInfo"] == {
        "title": "Brisa",
        "credit": "Ana",
        "durationSeconds": mine["durationSeconds"],
    }
    read = await published(client, created["id"])
    assert read["settings"]["audio"] == {
        "assetId": mine["id"],
        "volume": 0.7,
        "loop": True,
        "startAfterInteraction": False,
    }
    assert read["audioInfo"] == {
        "title": "Brisa",
        "credit": "Ana",
        "durationSeconds": mine["durationSeconds"],
        "available": True,
    }


async def test_music_is_reported_apart_from_the_other_settings(
    client: EditorClient,
) -> None:
    mine = await track(client)
    created = await make_map(client)
    await add_bubble(client, created["id"], 1)
    await publish(client, created["id"])
    assert (await state(client, created["id"]))["changes"]["audioChanged"] is False

    await set_music(client, created["id"], music(mine["id"]))
    added = await state(client, created["id"])

    assert added["status"] == "unpublished_changes"
    assert added["changes"]["audioChanged"] is True
    assert added["changes"]["settingsChanged"] is False

    await publish(client, created["id"])
    assert (await state(client, created["id"]))["status"] == "up_to_date"

    await set_music(client, created["id"], music(mine["id"], volume=0.9))
    louder = await state(client, created["id"])
    assert louder["changes"]["audioChanged"] is True
    await publish(client, created["id"])

    await set_music(client, created["id"], None)
    removed = await state(client, created["id"])
    assert removed["changes"]["audioChanged"] is True
    assert removed["changes"]["settingsChanged"] is False


async def test_a_published_map_keeps_the_info_it_was_published_with(
    client: EditorClient,
) -> None:
    mine = await track(client, title="Antes", credit="Ana")
    created = await with_music(client, mine["id"])
    await publish(client, created["id"])

    await client.patch(f"/assets/{mine['id']}", json={"title": "Después"})

    assert (await published(client, created["id"]))["audioInfo"]["title"] == "Antes"
    assert (await draft(client, created["id"]))["audioInfo"]["title"] == "Después"
    changed = await state(client, created["id"])  # a new title is a change
    assert changed["status"] == "unpublished_changes"
    assert changed["changes"]["audioChanged"] is True


async def test_students_read_the_music_of_a_published_map(
    engine: AsyncEngine, make_client: MakeClient
) -> None:
    tenant = await insert_tenant(engine, "alpha")
    editor = make_client(await insert_token(engine, tenant, 7, Role.EDITOR))
    student = make_client(await insert_token(engine, tenant, 7, Role.STUDENT))
    shared = await library_track(engine, title="Brisa", credit="Ana")
    created = await make_map(editor, 7)
    await set_music(editor, created["id"], music(str(shared.id)))
    await publish(editor, created["id"])

    read = await published(student, created["id"])

    assert read["settings"]["audio"]["assetId"] == str(shared.id)
    assert read["audioInfo"]["title"] == "Brisa"


# --- takedown ------------------------------------------------------------------------


async def test_a_taken_down_track_leaves_maps_working_without_music(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client, title="Brisa")
    created = await with_music(client, mine["id"])
    await publish(client, created["id"])

    await quarantine(engine, mine["id"], "complaint")

    # Students: the published map plays without music; its snapshot is intact.
    read = await published(client, created["id"])
    assert read["settings"]["audio"] is None and read["audioInfo"] is None
    async with new_session(engine, SYSTEM) as s:
        snapshot = (await s.scalars(select(Publication.snapshot))).one()
    assert snapshot["settings"]["audio"]["assetId"] == mine["id"]
    assert snapshot["audioInfo"]["title"] == "Brisa"
    # Editors: the map still opens, with no info to show.
    body = await draft(client, created["id"])
    assert body["audioInfo"] is None
    # The draft is now effectively without music: publishing says so.
    changed = await state(client, created["id"])
    assert changed["status"] == "unpublished_changes"
    assert changed["changes"]["audioChanged"] is True


async def test_republishing_after_a_takedown_publishes_no_music(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])
    await publish(client, created["id"])
    await quarantine(engine, mine["id"])

    await publish(client, created["id"])

    async with new_session(engine, SYSTEM) as s:
        latest = (
            await s.scalars(select(Publication).order_by(Publication.number.desc()))
        ).first()
    assert latest is not None
    assert "audioInfo" not in latest.snapshot
    assert "audio" not in latest.snapshot["settings"]
    assert latest.asset_ids == []


async def test_saving_the_appearance_again_with_a_taken_down_track_is_refused(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])
    await quarantine(engine, mine["id"])

    again = await set_music(client, created["id"], music(mine["id"]))
    cleared = await set_music(client, created["id"], None)

    assert again.status_code == 422
    assert again.json()["detail"]["code"] == "audio_asset_not_found"
    assert cleared.status_code == 200


async def test_releasing_a_track_brings_the_music_back(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])
    await publish(client, created["id"])
    await quarantine(engine, mine["id"])
    assert (await published(client, created["id"]))["audioInfo"] is None

    await release(engine, mine["id"])

    assert (await published(client, created["id"]))["audioInfo"] is not None
    assert (await draft(client, created["id"]))["audioInfo"] is not None
    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_a_deleted_track_is_no_music_either(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])
    async with new_session(engine, SYSTEM) as s:
        await s.execute(delete(Asset).where(Asset.id == uuid.UUID(mine["id"])))
        await s.commit()

    assert (await draft(client, created["id"]))["audioInfo"] is None
    await publish(client, created["id"])  # nothing to freeze, nothing to fail
    read = await published(client, created["id"])
    assert read["settings"]["audio"] is None


# --- discard and restore --------------------------------------------------------------


async def test_discarding_changes_brings_the_published_music_back(
    client: EditorClient,
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"], volume=0.8)
    await publish(client, created["id"])
    await set_music(client, created["id"], None)

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []
    restored = r.json()["map"]
    assert restored["settings"]["audio"]["assetId"] == mine["id"]
    assert restored["settings"]["audio"]["volume"] == 0.8
    assert restored["audioInfo"]["title"] == "Brisa"
    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_discarding_changes_clears_music_when_it_was_published_without(
    client: EditorClient,
) -> None:
    mine = await track(client)
    created = await make_map(client)
    await add_bubble(client, created["id"], 1)
    await publish(client, created["id"])
    await set_music(client, created["id"], music(mine["id"]))

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200
    assert r.json()["map"]["settings"]["audio"] is None
    assert r.json()["map"]["audioInfo"] is None


async def test_restoring_with_a_taken_down_track_warns_and_drops_the_music(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])
    await publish(client, created["id"])
    await quarantine(engine, mine["id"])

    r = await client.post(f"/course-maps/{created['id']}/publications/1/restore")

    assert r.status_code == 200, r.text
    assert [w["code"] for w in r.json()["warnings"]] == ["audio_unavailable"]
    assert r.json()["map"]["settings"]["audio"] is None
    assert r.json()["map"]["audioInfo"] is None
    assert (await draft(client, created["id"]))["settings"]["audio"] is None


async def test_restoring_with_a_deleted_track_warns_and_drops_the_music(
    client: EditorClient, engine: AsyncEngine
) -> None:
    mine = await track(client)
    created = await with_music(client, mine["id"])
    await publish(client, created["id"])
    async with new_session(engine, SYSTEM) as s:
        await s.execute(delete(Asset).where(Asset.id == uuid.UUID(mine["id"])))
        await s.commit()

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200, r.text
    assert [w["code"] for w in r.json()["warnings"]] == ["audio_unavailable"]
    assert r.json()["map"]["settings"]["audio"] is None


async def test_restoring_an_older_version_restores_its_music(
    client: EditorClient,
) -> None:
    first = await track(client, 1, title="Primera")
    second = await track(client, 2, title="Segunda")
    created = await with_music(client, first["id"])
    await publish(client, created["id"])
    await set_music(client, created["id"], music(second["id"]))
    await publish(client, created["id"])

    r = await client.post(f"/course-maps/{created['id']}/publications/1/restore")

    assert r.status_code == 200 and r.json()["warnings"] == []
    assert r.json()["map"]["audioInfo"]["title"] == "Primera"
    # It is only in the draft: nothing was published by restoring.
    assert (await published(client, created["id"]))["audioInfo"]["title"] == "Segunda"
