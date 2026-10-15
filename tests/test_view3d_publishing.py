"""The 3D view through draft, publish, history, discard and restore."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from app.view3d import scenes
from tests.api_client import ApiClient
from tests.fakes import InMemoryMoodleClient

SCENE = "region-encuentros"


@pytest.fixture
async def plain(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """A client that sends no `If-Match` by itself."""
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with ApiClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


async def make_map(client: httpx.AsyncClient, course: int = 1) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={"title": "Mapa", "moodleCourseId": course, "imageUrl": "http://i/x.png"},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def add_bubble(
    client: httpx.AsyncClient, map_id: str, activity: int
) -> dict[str, Any]:
    r = await client.post(
        f"/course-maps/{map_id}/bubbles",
        json={"activityId": activity, "x": 0.1 * (activity % 9 + 1), "y": 0.5},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def set_view3d(
    client: httpx.AsyncClient, map_id: str, payload: dict[str, Any] | None
) -> dict[str, Any]:
    r = await client.put(
        f"/course-maps/{map_id}/view3d",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def view(*pairs: tuple[int, str], **first: Any) -> dict[str, Any]:
    """Scene with the given (zone, bubble) bindings; `first` adds fields to the
    first binding (a title, say)."""
    bindings = [{"zoneId": z, "bubbleId": b} for z, b in pairs]
    if bindings:
        bindings[0].update(first)
    return {"scene": SCENE, "bindings": bindings}


async def state(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    r = await client.get(f"/course-maps/{map_id}/publication-state")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def publish(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    current = await state(client, map_id)
    r = await client.post(
        f"/course-maps/{map_id}/publish", json={"draftHash": current["draftHash"]}
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def published(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    r = await client.get(f"/published/course-maps/{map_id}")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def draft(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(f"/course-maps/{map_id}")).json()
    return body


# --- state and diff -----------------------------------------------------------


async def test_a_map_published_without_a_3d_view_stays_up_to_date(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await add_bubble(client, created["id"], 1)
    await publish(client, created["id"])

    current = await state(client, created["id"])

    assert current["status"] == "up_to_date"
    assert current["changes"]["view3dChanged"] is False


async def test_the_3d_view_makes_unpublished_changes_only_when_it_changes(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    await publish(client, created["id"])

    await set_view3d(client, created["id"], view((0, a["id"])))
    added = await state(client, created["id"])
    assert added["status"] == "unpublished_changes"
    assert added["changes"]["view3dChanged"] is True
    # Nothing else is reported as changed.
    assert {k: v for k, v in added["changes"].items() if k != "view3dChanged"} == {
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

    await publish(client, created["id"])
    assert (await state(client, created["id"]))["status"] == "up_to_date"

    # Only a text changes: still a change to the 3D view.
    await set_view3d(client, created["id"], view((0, a["id"]), title={"es": "Hola"}))
    reworded = await state(client, created["id"])
    assert reworded["status"] == "unpublished_changes"
    assert reworded["changes"]["view3dChanged"] is True


async def test_putting_back_the_same_view_is_not_a_change(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    await set_view3d(client, created["id"], view((0, a["id"])))
    await publish(client, created["id"])

    await set_view3d(client, created["id"], view((0, a["id"])))  # revision moves on

    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_removing_the_3d_view_of_a_published_map_is_a_change(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    await set_view3d(client, created["id"], view((0, a["id"])))
    await publish(client, created["id"])

    await set_view3d(client, created["id"], None)

    current = await state(client, created["id"])
    assert current["status"] == "unpublished_changes"
    assert current["changes"]["view3dChanged"] is True


async def test_the_map_list_shows_unpublished_3d_changes(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client, course=4)
    a = await add_bubble(client, created["id"], 1)
    await publish(client, created["id"])
    await set_view3d(client, created["id"], view((0, a["id"])))

    listed = await client.get("/course-maps", params={"moodleCourseId": 4})

    [item] = listed.json()["items"]
    assert item["publication"]["status"] == "unpublished_changes"


# --- what students see --------------------------------------------------------------


async def test_the_published_read_has_a_null_view3d_without_a_3d_view(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await publish(client, created["id"])

    assert (await published(client, created["id"]))["view3d"] is None


async def test_the_published_read_returns_the_frozen_3d_config(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    b = await add_bubble(client, created["id"], 2)
    await set_view3d(
        client,
        created["id"],
        {
            "scene": SCENE,
            "bindings": [
                {
                    "zoneId": 6,
                    "bubbleId": b["id"],
                    "description": {"en": "Meet here"},
                },
                {"zoneId": 1, "bubbleId": a["id"], "title": {"es": "Inicio"}},
            ],
        },
    )
    await publish(client, created["id"])
    frozen = {
        "scene": SCENE,
        "bindings": [
            {
                "zoneId": 1,
                "bubbleId": a["id"],
                "title": {"es": "Inicio"},
                "description": None,
            },
            {
                "zoneId": 6,
                "bubbleId": b["id"],
                "title": None,
                "description": {"en": "Meet here"},
            },
        ],
    }
    assert (await published(client, created["id"]))["view3d"] == frozen

    # The draft moves on in every way: the students' copy does not.
    await set_view3d(client, created["id"], view((3, b["id"])))
    await client.delete(f"/course-maps/{created['id']}/bubbles/{a['id']}")
    after = await published(client, created["id"])
    assert after["view3d"] == frozen
    # ...and it keeps being served after the 3D view is removed from the draft.
    await set_view3d(client, created["id"], None)
    assert (await published(client, created["id"]))["view3d"] == frozen


async def test_the_next_publication_replaces_the_frozen_view(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    await set_view3d(client, created["id"], view((0, a["id"])))
    await publish(client, created["id"])
    await set_view3d(client, created["id"], None)

    await publish(client, created["id"])

    assert (await published(client, created["id"]))["view3d"] is None


# --- publish validation -----------------------------------------------------------


async def test_publishing_a_draft_with_an_unknown_scene_is_publish_invalid(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    created = await make_map(client)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE course_maps SET view3d_scene = 'gone' WHERE id = :i"),
            {"i": created["id"]},
        )
    current = await state(client, created["id"])

    r = await client.post(
        f"/course-maps/{created['id']}/publish",
        json={"draftHash": current["draftHash"]},
    )

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "publish_invalid"
    assert [p["code"] for p in detail["problems"]] == ["scene_unknown"]
    assert (
        await client.get(f"/published/course-maps/{created['id']}")
    ).status_code == (404)


async def test_a_scene_removed_from_the_registry_blocks_the_next_publish(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await make_map(client)
    await set_view3d(client, created["id"], view())
    monkeypatch.setattr(scenes, "SCENES", {"other": 3})
    current = await state(client, created["id"])

    r = await client.post(
        f"/course-maps/{created['id']}/publish",
        json={"draftHash": current["draftHash"]},
    )

    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "scene_unknown"


# --- discard and restore -----------------------------------------------------------


async def test_discard_changes_brings_back_the_published_3d_view(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    b = await add_bubble(client, created["id"], 2)
    await set_view3d(
        client,
        created["id"],
        view((2, a["id"]), (5, b["id"]), title={"es": "Inicio", "en": "Start"}),
    )
    await publish(client, created["id"])
    published_view = (await draft(client, created["id"]))["view3d"]
    await set_view3d(client, created["id"], view((0, b["id"])))

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []
    assert r.json()["map"]["view3d"] == published_view
    assert (await draft(client, created["id"]))["view3d"] == published_view
    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_discard_changes_removes_a_3d_view_added_after_publishing(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    await publish(client, created["id"])
    await set_view3d(client, created["id"], view((0, a["id"])))

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200, r.text
    assert r.json()["map"]["view3d"] is None
    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_restore_loads_an_older_publications_3d_view(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    await publish(client, created["id"])  # 1: no 3D view
    await set_view3d(client, created["id"], view((4, a["id"]), title={"es": "Dos"}))
    await publish(client, created["id"])  # 2
    two = (await draft(client, created["id"]))["view3d"]

    to_one = await client.post(f"/course-maps/{created['id']}/publications/1/restore")
    assert to_one.status_code == 200, to_one.text
    assert to_one.json()["map"]["view3d"] is None

    to_two = await client.post(f"/course-maps/{created['id']}/publications/2/restore")
    assert to_two.json()["map"]["view3d"] == two
    assert to_two.json()["warnings"] == []


async def test_restore_recreates_deleted_bubbles_and_their_bindings(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    keep = await add_bubble(client, created["id"], 1)
    doomed = await add_bubble(client, created["id"], 2)
    await set_view3d(
        client,
        created["id"],
        view((0, keep["id"]), (1, doomed["id"]), title={"es": "Uno"}),
    )
    await publish(client, created["id"])
    published_view = (await draft(client, created["id"]))["view3d"]
    await client.delete(f"/course-maps/{created['id']}/bubbles/{doomed['id']}")
    assert len((await draft(client, created["id"]))["view3d"]["bindings"]) == 1

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []
    # The bubble is back with its original id, so its binding could return.
    assert r.json()["map"]["view3d"] == published_view
    assert (await state(client, created["id"]))["status"] == "up_to_date"


async def test_a_binding_whose_bubble_cannot_come_back_is_left_out(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client, course=1)
    other = await make_map(client, course=1)
    keep = await add_bubble(client, created["id"], 1)
    doomed = await add_bubble(client, created["id"], 2)
    await set_view3d(client, created["id"], view((0, keep["id"]), (1, doomed["id"])))
    await publish(client, created["id"])
    await client.delete(f"/course-maps/{created['id']}/bubbles/{doomed['id']}")
    await add_bubble(client, other["id"], 2)  # the activity is placed elsewhere now

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200, r.text
    assert [w["code"] for w in r.json()["warnings"]] == ["bubble_not_restored"]
    view_after = r.json()["map"]["view3d"]
    assert [b["bubbleId"] for b in view_after["bindings"]] == [keep["id"]]


async def test_restoring_an_unknown_scene_drops_the_3d_view_with_a_warning(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    await set_view3d(client, created["id"], view((0, a["id"])))
    await publish(client, created["id"])
    revision_before = (await draft(client, created["id"]))["revision"]
    monkeypatch.setattr(scenes, "SCENES", {"other": 3})

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200, r.text
    result = r.json()
    assert [w["code"] for w in result["warnings"]] == ["scene_unknown"]
    assert result["map"]["view3d"] is None
    assert result["map"]["revision"] == revision_before + 1
    assert result["map"]["bubbles"][0]["id"] == a["id"]  # the rest was restored
    assert (await draft(client, created["id"]))["view3d"] is None


async def test_restore_leaves_out_zones_a_scene_no_longer_has(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await make_map(client)
    a = await add_bubble(client, created["id"], 1)
    b = await add_bubble(client, created["id"], 2)
    await set_view3d(client, created["id"], view((1, a["id"]), (8, b["id"])))
    await publish(client, created["id"])
    monkeypatch.setattr(scenes, "SCENES", {SCENE: 4})

    r = await client.post(f"/course-maps/{created['id']}/discard-changes")

    assert r.status_code == 200, r.text
    assert [b["zoneId"] for b in r.json()["map"]["view3d"]["bindings"]] == [1]


async def test_restore_and_discard_still_need_a_current_revision(
    plain: httpx.AsyncClient,
) -> None:
    created = await make_map(plain)
    a = await add_bubble(plain, created["id"], 1)
    await publish(plain, created["id"])
    await plain.put(
        f"/course-maps/{created['id']}/view3d",
        json=view((0, a["id"])),
        headers={"If-Match": '"1"'},
    )

    stale = await plain.post(
        f"/course-maps/{created['id']}/discard-changes", headers={"If-Match": '"1"'}
    )

    assert stale.status_code == 412
    assert (await draft(plain, created["id"]))["view3d"] is not None
