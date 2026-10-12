"""Publishing through the real stack: state, publish, history, discard, restore."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import get_settings
from app.main import app
from app.models import Asset, Publication
from tests import skin_configs
from tests.api_client import ApiClient
from tests.asset_helpers import png_asset
from tests.fakes import InMemoryMoodleClient

HEX64 = "0" * 64


@pytest.fixture
async def plain(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """A client that sends no `If-Match` by itself."""
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with ApiClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


@pytest.fixture
def set_keep() -> Iterator[Callable[[int], None]]:
    def apply(keep: int) -> None:
        app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
            update={"publications_keep": keep}
        )

    yield apply
    app.dependency_overrides.pop(get_settings, None)


def match(number: int) -> dict[str, str]:
    return {"If-Match": f'"{number}"'}


async def make_map(
    client: httpx.AsyncClient, course: int = 1, image_url: str = "http://i/x.png"
) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={"title": "Mapa", "moodleCourseId": course, "imageUrl": image_url},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def add_bubble(
    client: httpx.AsyncClient, map_id: str, activity: int, **fields: Any
) -> dict[str, Any]:
    skin_id = fields.pop("skinId", None)  # not part of creation: set by PATCH
    r = await client.post(
        f"/course-maps/{map_id}/bubbles",
        json={
            "activityId": activity,
            "x": 0.05 + 0.09 * (activity % 10),
            "y": 0.5,
            **fields,
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    if skin_id is not None:
        patched = await client.patch(
            f"/course-maps/{map_id}/bubbles/{body['id']}", json={"skinId": skin_id}
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
    return body


async def make_skin(client: httpx.AsyncClient, name: str = "Skin") -> dict[str, Any]:
    r = await client.post(
        "/skins", json={"name": name, "config": skin_configs.procedural()}
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def state(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    r = await client.get(f"/course-maps/{map_id}/publication-state")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def publish(
    client: httpx.AsyncClient, map_id: str, note: str | None = None
) -> dict[str, Any]:
    """Publish what the editor sees: review the state, then publish its hash."""
    current = await state(client, map_id)
    r = await client.post(
        f"/course-maps/{map_id}/publish",
        json={"draftHash": current["draftHash"], "note": note},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def full_map(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(f"/course-maps/{map_id}")).json()
    return body


NO_CHANGES = {
    "backgroundChanged": False,
    "titleChanged": False,
    "settingsChanged": False,
    "skinsChanged": False,
    "bubblesAdded": 0,
    "bubblesRemoved": 0,
    "bubblesMoved": 0,
    "bubblesRestyled": 0,
    "bubblesReordered": False,
}


# --- publication-state --------------------------------------------------------------


async def test_a_new_map_was_never_published(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    body = await state(client, created["id"])

    assert body["status"] == "never_published"
    assert body["changes"] is None
    assert (body["currentNumber"], body["currentPublishedAt"], body["currentNote"]) == (
        None,
        None,
        None,
    )
    assert len(body["draftHash"]) == 64
    assert set(body) == {
        "status",
        "currentNumber",
        "currentPublishedAt",
        "currentNote",
        "draftHash",
        "changes",
    }


async def test_creating_a_map_creates_no_publication(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await make_map(client)

    async with AsyncSession(engine) as s:
        assert await s.scalar(select(func.count()).select_from(Publication)) == 0


async def test_state_of_a_missing_map_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/course-maps/{uuid.uuid4()}/publication-state")

    assert r.status_code == 404


async def test_the_draft_hash_is_stable_until_the_draft_changes(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    first = (await state(client, created["id"]))["draftHash"]
    again = (await state(client, created["id"]))["draftHash"]
    await client.patch(f"/course-maps/{created['id']}", json={"title": "Otro"})
    changed = (await state(client, created["id"]))["draftHash"]

    assert first == again != changed


async def test_bubble_status_is_live_data_and_never_changes_the_hash(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await add_bubble(client, created["id"], 1)
    before = (await state(client, created["id"]))["draftHash"]

    await client.patch(
        f"/course-maps/{created['id']}/bubbles/{bubble['id']}",
        json={"status": "complete"},
    )

    assert (await state(client, created["id"]))["draftHash"] == before


# --- publish --------------------------------------------------------------------------


async def test_publish_returns_the_summary_and_marks_the_map_up_to_date(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await add_bubble(client, created["id"], 1)
    await add_bubble(client, created["id"], 2)
    reviewed = await state(client, created["id"])

    r = await client.post(
        f"/course-maps/{created['id']}/publish",
        json={"draftHash": reviewed["draftHash"], "note": "  Primera   versión "},
    )

    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"number", "createdAt", "note", "title", "bubbleCount"}
    assert (body["number"], body["note"], body["title"], body["bubbleCount"]) == (
        1,
        "Primera versión",
        "Mapa",
        2,
    )
    after = await state(client, created["id"])
    assert after["status"] == "up_to_date"
    assert (after["currentNumber"], after["currentNote"]) == (1, "Primera versión")
    assert after["currentPublishedAt"] == body["createdAt"]
    assert after["draftHash"] == reviewed["draftHash"]
    assert after["changes"] == NO_CHANGES


async def test_publishing_twice_without_changes_is_409(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await publish(client, created["id"])
    current = await state(client, created["id"])

    r = await client.post(
        f"/course-maps/{created['id']}/publish",
        json={"draftHash": current["draftHash"]},
    )

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "no_changes_to_publish"
    assert (
        len(
            (await client.get(f"/course-maps/{created['id']}/publications")).json()[
                "items"
            ]
        )
        == 1
    )


async def test_the_full_cycle_publish_edit_review_publish(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    kept = await add_bubble(client, map_id, 1)
    moved = await add_bubble(client, map_id, 2)
    restyled = await add_bubble(client, map_id, 3)
    removed = await add_bubble(client, map_id, 4)
    await publish(client, map_id, "v1")

    # Edit: move one, restyle one, delete one, add one, retitle, new background.
    base = f"/course-maps/{map_id}/bubbles"
    assert (
        await client.patch(f"{base}/{moved['id']}", json={"x": 0.9, "y": 0.9})
    ).status_code == 200
    assert (
        await client.patch(f"{base}/{restyled['id']}", json={"icon": "star"})
    ).status_code == 200
    assert (await client.delete(f"{base}/{removed['id']}")).status_code == 204
    await add_bubble(client, map_id, 5)
    assert (
        await client.patch(
            f"/course-maps/{map_id}",
            json={"title": "Nuevo", "imageUrl": "http://i/y.png"},
        )
    ).status_code == 200

    reviewing = await state(client, map_id)
    assert reviewing["status"] == "unpublished_changes"
    assert (reviewing["currentNumber"], reviewing["currentNote"]) == (1, "v1")
    assert reviewing["changes"] == {
        **NO_CHANGES,
        "backgroundChanged": True,
        "titleChanged": True,
        "bubblesAdded": 1,
        "bubblesRemoved": 1,
        "bubblesMoved": 1,
        "bubblesRestyled": 1,
    }
    assert kept["id"]  # untouched, counts nowhere

    second = await client.post(
        f"/course-maps/{map_id}/publish",
        json={"draftHash": reviewing["draftHash"], "note": "v2"},
    )
    assert second.status_code == 201 and second.json()["number"] == 2
    assert second.json()["title"] == "Nuevo" and second.json()["bubbleCount"] == 4
    assert (await state(client, map_id))["status"] == "up_to_date"

    history = (await client.get(f"/course-maps/{map_id}/publications")).json()["items"]
    assert [
        (h["number"], h["note"], h["title"], h["bubbleCount"]) for h in history
    ] == [
        (2, "v2", "Nuevo", 4),
        (1, "v1", "Mapa", 4),
    ]


async def test_a_path_reorder_alone_is_an_unpublished_change(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    first = await add_bubble(client, created["id"], 1)
    second = await add_bubble(client, created["id"], 2)
    await publish(client, created["id"])

    await client.put(
        f"/course-maps/{created['id']}/bubbles/order",
        json={"bubbleIds": [second["id"], first["id"]]},
    )

    reviewing = await state(client, created["id"])
    assert reviewing["status"] == "unpublished_changes"
    assert reviewing["changes"] == {**NO_CHANGES, "bubblesReordered": True}


async def test_a_bubble_moved_between_review_and_publish_is_409_with_the_new_hash(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await add_bubble(client, created["id"], 1)
    reviewed = await state(client, created["id"])
    await client.patch(
        f"/course-maps/{created['id']}/bubbles/{bubble['id']}",
        json={"x": 0.9, "y": 0.9},
    )

    r = await client.post(
        f"/course-maps/{created['id']}/publish",
        json={"draftHash": reviewed["draftHash"]},
    )

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "draft_changed_since_review"
    assert detail["currentHash"] == (await state(client, created["id"]))["draftHash"]
    assert (await client.get(f"/course-maps/{created['id']}/publications")).json()[
        "items"
    ] == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"draftHash": "abc"},
        {"draftHash": "A" * 64},
        {"draftHash": HEX64, "note": "n" * 201},
    ],
)
async def test_publish_validates_its_body(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    created = await make_map(client)

    r = await client.post(f"/course-maps/{created['id']}/publish", json=body)

    assert r.status_code == 422


async def test_publishing_a_missing_map_is_404(client: httpx.AsyncClient) -> None:
    r = await client.post(
        f"/course-maps/{uuid.uuid4()}/publish", json={"draftHash": HEX64}
    )

    assert r.status_code == 404


async def test_publish_lists_every_problem_when_assets_are_gone(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    background = await png_asset(client, 1, "background")
    bubble_a = await png_asset(client, 2, "bubble", size=(16, 16))
    bubble_b = await png_asset(client, 3, "bubble", size=(16, 16))
    created = await make_map(client, image_url=background["url"])
    skin = (
        await client.post(
            "/skins",
            json={
                "name": "Con imágenes",
                "config": skin_configs.image(bubble_a["id"], locked=bubble_b["id"]),
            },
        )
    ).json()
    await client.put(
        f"/course-maps/{created['id']}/appearance",
        json={"settings": {}, "defaultSkinId": skin["id"]},
    )
    reviewed = await state(client, created["id"])
    # Assets cannot be deleted while used; remove the rows behind the API's back.
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM assets WHERE id = ANY(:ids)"),
            {"ids": [uuid.UUID(background["id"]), uuid.UUID(bubble_a["id"])]},
        )

    r = await client.post(
        f"/course-maps/{created['id']}/publish",
        json={"draftHash": reviewed["draftHash"]},
    )

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "publish_invalid"
    codes = sorted(p["code"] for p in detail["problems"])
    assert codes == ["background_asset_missing", "skin_asset_missing"]
    assert all(p["message"] for p in detail["problems"])
    assert (await client.get(f"/course-maps/{created['id']}/publications")).json()[
        "items"
    ] == []


async def test_old_publications_are_pruned_but_never_the_latest(
    client: httpx.AsyncClient, set_keep: Callable[[int], None]
) -> None:
    set_keep(2)
    created = await make_map(client)
    for n in range(1, 5):
        await client.patch(f"/course-maps/{created['id']}", json={"title": f"v{n}"})
        await publish(client, created["id"])

    items = (await client.get(f"/course-maps/{created['id']}/publications")).json()[
        "items"
    ]
    assert [i["number"] for i in items] == [4, 3]

    set_keep(1)
    await client.patch(f"/course-maps/{created['id']}", json={"title": "v5"})
    assert (await publish(client, created["id"]))["number"] == 5  # numbers never reused
    items = (await client.get(f"/course-maps/{created['id']}/publications")).json()[
        "items"
    ]
    assert [i["number"] for i in items] == [5]
    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_the_history_limit_is_validated_and_applied(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    for n in range(3):
        await client.patch(f"/course-maps/{created['id']}", json={"title": f"v{n}"})
        await publish(client, created["id"])
    url = f"/course-maps/{created['id']}/publications"

    assert len((await client.get(url, params={"limit": 2})).json()["items"]) == 2
    assert (await client.get(url, params={"limit": 0})).status_code == 422
    assert (await client.get(url, params={"limit": 101})).status_code == 422
    assert (
        await client.get(f"/course-maps/{uuid.uuid4()}/publications")
    ).status_code == 404


async def test_two_simultaneous_publishes_of_the_same_draft_make_one_publication(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await add_bubble(client, created["id"], 1)
    reviewed = await state(client, created["id"])

    results = await asyncio.gather(
        *(
            client.post(
                f"/course-maps/{created['id']}/publish",
                json={"draftHash": reviewed["draftHash"]},
            )
            for _ in range(3)
        )
    )

    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1
    assert all(c == 409 for c in codes if c != 201), codes
    items = (await client.get(f"/course-maps/{created['id']}/publications")).json()[
        "items"
    ]
    assert [i["number"] for i in items] == [1]


async def test_deleting_a_map_deletes_its_publications(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    created = await make_map(client)
    await publish(client, created["id"])

    assert (await client.delete(f"/course-maps/{created['id']}")).status_code == 204

    async with AsyncSession(engine) as s:
        assert await s.scalar(select(func.count()).select_from(Publication)) == 0


# --- skins are frozen ---------------------------------------------------------------------


async def test_a_skin_edited_after_publishing_is_an_unpublished_change(
    client: httpx.AsyncClient,
) -> None:
    skin = await make_skin(client)
    created = await make_map(client)
    await client.put(
        f"/course-maps/{created['id']}/appearance",
        json={"settings": {}, "defaultSkinId": skin["id"]},
    )
    await publish(client, created["id"])
    assert (await state(client, created["id"]))["status"] == "up_to_date"

    await client.patch(f"/skins/{skin['id']}", json={"name": "Renombrada"})

    reviewing = await state(client, created["id"])
    assert reviewing["status"] == "unpublished_changes"
    assert reviewing["changes"] == {**NO_CHANGES, "skinsChanged": True}


# --- discard and restore ---------------------------------------------------------------------


async def test_discard_without_a_publication_is_409_never_published(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "never_published"


async def test_discard_brings_the_published_content_back_into_the_draft(
    client: httpx.AsyncClient,
) -> None:
    skin = await make_skin(client)
    created = await make_map(client)
    map_id = created["id"]
    await client.put(
        f"/course-maps/{map_id}/appearance",
        json={
            "settings": {"mode": "guided"},
            "defaultSkinId": skin["id"],
            "skinRules": [{"modname": "quiz", "skinId": skin["id"]}],
        },
    )
    kept = await add_bubble(client, map_id, 1, icon="flag")
    moved = await add_bubble(client, map_id, 2)
    deleted = await add_bubble(client, map_id, 3, skinId=skin["id"])
    published = await full_map(client, map_id)
    await publish(client, map_id)

    # Wreck the draft.
    base = f"/course-maps/{map_id}/bubbles"
    await client.patch(f"{base}/{moved['id']}", json={"x": 0.99, "y": 0.01})
    await client.delete(f"{base}/{deleted['id']}")
    extra = await add_bubble(client, map_id, 9)
    await client.patch(
        f"/course-maps/{map_id}", json={"title": "Roto", "imageUrl": "/zz"}
    )
    await client.put(f"/course-maps/{map_id}/appearance", json={"settings": {}})
    assert (await state(client, map_id))["status"] == "unpublished_changes"
    before = await full_map(client, map_id)

    r = await client.post(
        f"/course-maps/{map_id}/discard-changes", headers=match(before["revision"])
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warnings"] == []
    restored = body["map"]
    assert r.headers["etag"] == f'"{restored["revision"]}"'
    assert restored["revision"] == before["revision"] + 1
    assert restored["title"] == "Mapa" and restored["imageUrl"] == "http://i/x.png"
    assert restored["settings"]["mode"] == "guided"
    assert restored["defaultSkinId"] == skin["id"]
    assert restored["skinRules"] == [{"modname": "quiz", "skinId": skin["id"]}]
    by_id = {b["id"]: b for b in restored["bubbles"]}
    # The deleted bubble is back under its ORIGINAL id; the added one is gone.
    assert set(by_id) == {kept["id"], moved["id"], deleted["id"]}
    assert extra["id"] not in by_id
    for bubble in published["bubbles"]:
        got = by_id[bubble["id"]]
        for field in ("activityId", "x", "y", "icon", "skinId", "sequence"):
            assert got[field] == pytest.approx(bubble[field]), field
    assert (await state(client, map_id))["status"] == "up_to_date"
    assert (await full_map(client, map_id))["revision"] == restored["revision"]


async def test_restore_bumps_only_the_versions_of_bubbles_that_changed(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    untouched = await add_bubble(client, map_id, 1)
    moved = await add_bubble(client, map_id, 2)
    await publish(client, map_id)
    await client.patch(
        f"/course-maps/{map_id}/bubbles/{moved['id']}", json={"x": 0.9, "y": 0.9}
    )
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/publications/1/restore", headers=match(revision)
    )

    versions = {b["id"]: b["version"] for b in r.json()["map"]["bubbles"]}
    assert versions[untouched["id"]] == 1
    assert versions[moved["id"]] == 3  # edited once (2), restored once (3)


async def test_restoring_an_older_version_does_not_publish_it(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await publish(client, map_id, "v1")
    await client.patch(f"/course-maps/{map_id}", json={"title": "Segunda"})
    await publish(client, map_id, "v2")
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/publications/1/restore", headers=match(revision)
    )

    assert r.status_code == 200 and r.json()["map"]["title"] == "Mapa"
    reviewing = await state(client, map_id)
    assert reviewing["status"] == "unpublished_changes"
    assert reviewing["currentNumber"] == 2  # students still see v2
    assert reviewing["changes"] == {**NO_CHANGES, "titleChanged": True}
    items = (await client.get(f"/course-maps/{map_id}/publications")).json()["items"]
    assert [i["number"] for i in items] == [2, 1]


async def test_restoring_with_a_deleted_skin_resets_it_and_warns(
    client: httpx.AsyncClient,
) -> None:
    keeper = await make_skin(client, "Se queda")
    doomed = await make_skin(client, "Se va")
    created = await make_map(client)
    map_id = created["id"]
    await client.put(
        f"/course-maps/{map_id}/appearance",
        json={
            "settings": {},
            "defaultSkinId": doomed["id"],
            "skinRules": [
                {"modname": "quiz", "skinId": doomed["id"]},
                {"modname": "url", "skinId": keeper["id"]},
            ],
        },
    )
    on_bubble = await add_bubble(client, map_id, 1, skinId=doomed["id"])
    on_keeper = await add_bubble(client, map_id, 2, skinId=keeper["id"])
    await publish(client, map_id)
    await client.patch(f"/course-maps/{map_id}", json={"title": "Cambiado"})
    assert (await client.delete(f"/skins/{doomed['id']}")).status_code == 204
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/publications/1/restore", headers=match(revision)
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["map"]["title"] == "Mapa"
    assert body["map"]["defaultSkinId"] is None
    assert body["map"]["skinRules"] == [{"modname": "url", "skinId": keeper["id"]}]
    by_id = {b["id"]: b for b in body["map"]["bubbles"]}
    assert by_id[on_bubble["id"]]["skinId"] is None
    assert by_id[on_keeper["id"]]["skinId"] == keeper["id"]
    warnings = body["warnings"]
    assert len(warnings) == 3  # default skin, the quiz rule, one bubble
    assert {w["code"] for w in warnings} == {"skin_missing"}
    assert all(doomed["id"] in w["message"] for w in warnings)


async def test_restore_fails_and_changes_nothing_when_the_background_is_gone(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    background = await png_asset(client, 1, "background")
    created = await make_map(client, image_url=background["url"])
    map_id = created["id"]
    await add_bubble(client, map_id, 1)
    await publish(client, map_id)
    await client.patch(
        f"/course-maps/{map_id}",
        json={"imageUrl": "http://i/other.png", "title": "Otro"},
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM assets WHERE id = :i"), {"i": uuid.UUID(background["id"])}
        )
    before = await full_map(client, map_id)

    r = await client.post(
        f"/course-maps/{map_id}/publications/1/restore",
        headers=match(before["revision"]),
    )

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "publish_invalid"
    assert [p["code"] for p in detail["problems"]] == ["background_asset_missing"]
    assert await full_map(client, map_id) == before  # revision included


async def test_a_bubble_whose_activity_moved_elsewhere_is_not_brought_back(
    client: httpx.AsyncClient,
) -> None:
    first = await make_map(client, course=5)
    second = await make_map(client, course=5)
    bubble = await add_bubble(client, first["id"], 7)
    await publish(client, first["id"])
    await client.delete(f"/course-maps/{first['id']}/bubbles/{bubble['id']}")
    await add_bubble(client, second["id"], 7)  # the activity now lives on map two
    revision = (await full_map(client, first["id"]))["revision"]

    r = await client.post(
        f"/course-maps/{first['id']}/discard-changes", headers=match(revision)
    )

    assert r.status_code == 200
    assert r.json()["map"]["bubbles"] == []
    assert [w["code"] for w in r.json()["warnings"]] == ["bubble_not_restored"]


async def test_discard_and_restore_need_if_match_and_report_conflicts(
    plain: httpx.AsyncClient,
) -> None:
    created = await make_map(plain)
    map_id = created["id"]
    await publish(plain, map_id)
    urls = [
        f"/course-maps/{map_id}/discard-changes",
        f"/course-maps/{map_id}/publications/1/restore",
    ]

    for url in urls:
        missing = await plain.post(url)
        stale = await plain.post(url, headers=match(9))
        assert missing.status_code == 428
        assert missing.json()["detail"]["code"] == "precondition_required"
        assert stale.status_code == 412
        assert stale.json()["detail"]["currentRevision"] == 1
    ok = await plain.post(urls[0], headers=match(1))
    assert ok.status_code == 200 and ok.json()["map"]["revision"] == 2
    again = await plain.post(urls[0], headers=match(1))  # now stale
    assert again.status_code == 412 and again.json()["detail"]["currentRevision"] == 2


async def test_restoring_an_unknown_publication_or_map_is_404(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await publish(client, created["id"])

    unknown_number = await client.post(
        f"/course-maps/{created['id']}/publications/99/restore", headers=match(1)
    )
    unknown_map = await client.post(
        f"/course-maps/{uuid.uuid4()}/discard-changes", headers=match(1)
    )

    assert unknown_number.status_code == 404
    assert unknown_map.status_code == 404


# --- the editor list ---------------------------------------------------------------------------


async def test_the_map_list_carries_each_maps_publication_status(
    client: httpx.AsyncClient,
) -> None:
    never = await make_map(client, course=1)
    current = await make_map(client, course=1)
    dirty = await make_map(client, course=1)
    await publish(client, current["id"])
    await publish(client, dirty["id"])
    await client.patch(f"/course-maps/{dirty['id']}", json={"title": "Cambiado"})

    items = (await client.get("/course-maps", params={"moodleCourseId": 1})).json()[
        "items"
    ]

    by_id = {i["id"]: i["publication"] for i in items}
    assert set(by_id[never["id"]]) == {"status", "number", "publishedAt"}
    assert by_id[never["id"]] == {
        "status": "never_published",
        "number": None,
        "publishedAt": None,
    }
    assert by_id[current["id"]]["status"] == "up_to_date"
    assert by_id[current["id"]]["number"] == 1
    assert by_id[current["id"]]["publishedAt"] is not None
    assert by_id[dirty["id"]]["status"] == "unpublished_changes"


async def test_the_map_list_computes_publication_status_in_a_fixed_number_of_queries(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    ids = []
    for course in range(1, 7):
        created = await make_map(client, course=course)
        await add_bubble(client, created["id"], 1)
        await publish(client, created["id"])
        ids.append(created["id"])
    statements: list[str] = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cur, stmt, *a: statements.append(stmt),
    )

    items = (await client.get("/course-maps")).json()["items"]

    assert len(items) == 6
    assert all(i["publication"]["status"] == "up_to_date" for i in items)
    # count + page + latest publications + 4 draft queries (+ skins): not per map.
    assert len(statements) <= 10, statements


async def test_reordering_maps_returns_their_publication_status_too(
    client: httpx.AsyncClient,
) -> None:
    one = await make_map(client, course=3)
    two = await make_map(client, course=3)
    await publish(client, one["id"])

    r = await client.put(
        "/course-maps/order",
        json={"moodleCourseId": 3, "mapIds": [two["id"], one["id"]]},
    )

    statuses = [i["publication"]["status"] for i in r.json()["items"]]
    assert statuses == ["never_published", "up_to_date"]


# --- assets used by publications --------------------------------------------------------------------


async def test_a_published_background_cannot_be_deleted_even_after_the_draft_moved_on(
    client: httpx.AsyncClient,
) -> None:
    background = await png_asset(client, 1, "background")
    created = await make_map(client, image_url=background["url"])
    await publish(client, created["id"])
    await client.patch(
        f"/course-maps/{created['id']}", json={"imageUrl": "http://i/z.png"}
    )

    listed = (await client.get("/assets")).json()["items"][0]
    refused = await client.delete(f"/assets/{background['id']}")

    assert listed["usage"] == {"maps": 0, "skins": 0, "publications": 1}
    assert refused.status_code == 409
    assert refused.json()["detail"]["usage"] == {
        "maps": 0,
        "skins": 0,
        "publications": 1,
    }


async def test_publications_count_once_per_publication_and_free_the_asset_when_pruned(
    client: httpx.AsyncClient, set_keep: Callable[[int], None]
) -> None:
    set_keep(1)
    background = await png_asset(client, 1, "background")
    created = await make_map(client, image_url=background["url"])
    await publish(client, created["id"])
    await client.patch(
        f"/course-maps/{created['id']}", json={"imageUrl": "http://i/z.png"}
    )
    await publish(client, created["id"])  # #2 prunes #1, the only user of the asset

    assert (await client.delete(f"/assets/{background['id']}")).status_code == 204


async def test_a_published_skin_image_cannot_be_deleted(
    client: httpx.AsyncClient,
) -> None:
    bubble_image = await png_asset(client, 1, "bubble")
    skin = (
        await client.post(
            "/skins",
            json={"name": "Imagen", "config": skin_configs.image(bubble_image["id"])},
        )
    ).json()
    created = await make_map(client)
    await client.put(
        f"/course-maps/{created['id']}/appearance",
        json={"settings": {}, "defaultSkinId": skin["id"]},
    )
    await publish(client, created["id"])
    # The skin goes away; the publication still depends on its frozen copy.
    await client.put(f"/course-maps/{created['id']}/appearance", json={"settings": {}})
    assert (await client.delete(f"/skins/{skin['id']}")).status_code == 204

    refused = await client.delete(f"/assets/{bubble_image['id']}")

    assert refused.status_code == 409
    assert refused.json()["detail"]["usage"] == {
        "maps": 0,
        "skins": 0,
        "publications": 1,
    }


async def test_published_assets_are_listed_in_the_publication_row(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    background = await png_asset(client, 1, "background")
    created = await make_map(client, image_url=background["url"])
    await publish(client, created["id"])

    async with AsyncSession(engine) as s:
        row = (await s.scalars(select(Publication))).one()
        assets = list(await s.scalars(select(Asset.id)))

    assert row.asset_ids == [uuid.UUID(background["id"])]
    assert row.asset_ids[0] in assets
