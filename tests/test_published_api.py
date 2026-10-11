"""The student-facing read API (`/published/...`) through the real stack."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from app.moodle.cache import CachedMoodleClient
from tests import skin_configs
from tests.editor_client import EditorClient
from tests.fakes import FakeClock, InMemoryMoodleClient
from tests.moodle_fixtures import course8, parse, raw_course8
from tests.test_publications_api import add_bubble, full_map, make_map, publish


@pytest.fixture
def moodle() -> InMemoryMoodleClient:
    fake = InMemoryMoodleClient()
    raw = raw_course8()
    raw[1]["visible"] = 1
    fake.courses[8] = parse(raw)
    return fake


@pytest.fixture
async def client(
    engine: AsyncEngine, moodle: InMemoryMoodleClient
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    # The real cache over the fake Moodle, as the lifespan wires it.
    app.state.moodle_client = CachedMoodleClient(
        moodle, ttl_seconds=60.0, stale_max_seconds=3600.0, clock=FakeClock()
    )
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t") as c:
        yield c


async def published_map(
    client: httpx.AsyncClient, course: int = 8, activities: tuple[int, ...] = (28,)
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    created = await make_map(client, course=course)
    bubbles = [await add_bubble(client, created["id"], a) for a in activities]
    await publish(client, created["id"])
    return created, bubbles


async def read(client: httpx.AsyncClient, map_id: str) -> dict[str, Any]:
    r = await client.get(f"/published/course-maps/{map_id}")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


# --- list -----------------------------------------------------------------------


async def test_the_student_list_hides_maps_that_were_never_published(
    client: httpx.AsyncClient,
) -> None:
    visible, _ = await published_map(client)
    await make_map(client, course=8)  # a draft nobody published

    body = (await client.get("/published/course-maps")).json()

    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [visible["id"]]
    assert (await client.get("/course-maps")).json()["total"] == 2  # editors see both


async def test_the_list_contract_and_live_counts(client: httpx.AsyncClient) -> None:
    created, bubbles = await published_map(client, activities=(28, 29, 30))
    await client.patch(
        f"/course-maps/{created['id']}/bubbles/{bubbles[0]['id']}",
        json={"status": "complete"},
    )

    [item] = (await client.get("/published/course-maps")).json()["items"]

    assert set(item) == {
        "id",
        "title",
        "moodleCourseId",
        "moodleSectionId",
        "position",
        "imageUrl",
        "bubbleCount",
        "completeCount",
        "publicationNumber",
        "publishedAt",
    }
    assert item["id"] == created["id"] and item["title"] == "Mapa"
    assert (item["bubbleCount"], item["completeCount"]) == (3, 1)
    assert item["publicationNumber"] == 1 and item["moodleSectionId"] is None


async def test_the_list_shows_the_published_content_not_the_draft(
    client: httpx.AsyncClient,
) -> None:
    created, bubbles = await published_map(client, activities=(28, 29))
    await client.patch(
        f"/course-maps/{created['id']}",
        json={"title": "Borrador", "imageUrl": "/n.png"},
    )
    await client.delete(f"/course-maps/{created['id']}/bubbles/{bubbles[0]['id']}")
    await add_bubble(client, created["id"], 30)

    [item] = (await client.get("/published/course-maps")).json()["items"]

    assert (item["title"], item["imageUrl"], item["bubbleCount"]) == (
        "Mapa",
        "http://i/x.png",
        2,
    )


async def test_the_list_orders_by_live_position_filters_and_pages(
    client: httpx.AsyncClient,
) -> None:
    first, _ = await published_map(client, course=8, activities=(28,))
    second, _ = await published_map(client, course=8, activities=(29,))
    other, _ = await published_map(client, course=9, activities=(30,))
    await client.put(
        "/course-maps/order",
        json={"moodleCourseId": 8, "mapIds": [second["id"], first["id"]]},
    )

    in_course = (
        await client.get("/published/course-maps", params={"moodleCourseId": 8})
    ).json()
    assert [i["id"] for i in in_course["items"]] == [second["id"], first["id"]]
    assert [i["position"] for i in in_course["items"]] == [0, 1]
    paged = (
        await client.get(
            "/published/course-maps",
            params={"moodleCourseId": 8, "limit": 1, "offset": 1},
        )
    ).json()
    assert (paged["total"], paged["limit"], paged["offset"]) == (2, 1, 1)
    assert [i["id"] for i in paged["items"]] == [first["id"]]
    assert (await client.get("/published/course-maps")).json()["total"] == 3
    assert other["id"] in {
        i["id"]
        for i in (
            await client.get("/published/course-maps", params={"moodleCourseId": 9})
        ).json()["items"]
    }


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_the_list_rejects_bad_paging(
    client: httpx.AsyncClient, params: dict[str, int]
) -> None:
    assert (
        await client.get("/published/course-maps", params=params)
    ).status_code == 422


# --- read ---------------------------------------------------------------------------


async def test_reading_an_unpublished_or_unknown_map_is_404_not_published(
    client: httpx.AsyncClient,
) -> None:
    draft = await make_map(client)

    for map_id in (draft["id"], str(uuid.uuid4())):
        for suffix in ("", "/resolved"):
            r = await client.get(f"/published/course-maps/{map_id}{suffix}")
            assert r.status_code == 404
            assert r.json()["detail"]["code"] == "not_published"


async def test_the_published_read_contract(client: httpx.AsyncClient) -> None:
    skin = (
        await client.post(
            "/skins", json={"name": "Mía", "config": skin_configs.procedural()}
        )
    ).json()
    created = await make_map(client, course=8)
    await client.put(
        f"/course-maps/{created['id']}/appearance",
        json={
            "settings": {"mode": "guided"},
            "defaultSkinId": skin["id"],
            "skinRules": [{"modname": "quiz", "skinId": skin["id"]}],
        },
    )
    first = await add_bubble(client, created["id"], 28, icon="star")
    await add_bubble(client, created["id"], 29)
    await publish(client, created["id"])

    body = await read(client, created["id"])

    assert set(body) == {
        "id",
        "title",
        "moodleCourseId",
        "moodleSectionId",
        "position",
        "imageUrl",
        "settings",
        "defaultSkinId",
        "skinRules",
        "skins",
        "bubbles",
        "publicationNumber",
        "publishedAt",
    }
    assert body["settings"]["mode"] == "guided"
    assert body["defaultSkinId"] == skin["id"]
    assert body["skinRules"] == [{"modname": "quiz", "skinId": skin["id"]}]
    assert body["publicationNumber"] == 1
    frozen = body["skins"][skin["id"]]
    assert set(frozen) == {"name", "isDefault", "config"}
    assert frozen["config"] == skin_configs.procedural()
    assert [b["sequence"] for b in body["bubbles"]] == [0, 1]
    assert set(body["bubbles"][0]) == {
        "id",
        "activityId",
        "x",
        "y",
        "icon",
        "skinId",
        "sequence",
        "status",
    }
    assert body["bubbles"][0]["id"] == first["id"]
    assert (body["bubbles"][0]["icon"], body["bubbles"][0]["status"]) == (
        "star",
        "locked",
    )


async def test_students_keep_seeing_the_published_version_while_editors_work(
    client: httpx.AsyncClient,
) -> None:
    created, bubbles = await published_map(client, activities=(28, 29))
    map_id = created["id"]
    before = await read(client, map_id)
    base = f"/course-maps/{map_id}/bubbles"
    await client.patch(f"{base}/{bubbles[0]['id']}", json={"x": 0.9, "y": 0.9})
    await client.delete(f"{base}/{bubbles[1]['id']}")
    await add_bubble(client, map_id, 30)
    await client.patch(f"/course-maps/{map_id}", json={"title": "Borrador"})

    def content(body: dict[str, Any]) -> dict[str, Any]:
        """The published map without live status (a deleted bubble reads
        `no_complete`, which is covered by its own test)."""
        return {
            **body,
            "bubbles": [
                {k: v for k, v in b.items() if k != "status"} for b in body["bubbles"]
            ],
        }

    assert content(await read(client, map_id)) == content(before)

    await publish(client, map_id)
    after = await read(client, map_id)
    assert after["title"] == "Borrador" and after["publicationNumber"] == 2
    assert len(after["bubbles"]) == 2


async def test_published_skins_are_frozen_even_if_the_skin_is_edited_or_deleted(
    client: httpx.AsyncClient,
) -> None:
    skin = (
        await client.post(
            "/skins", json={"name": "Original", "config": skin_configs.procedural()}
        )
    ).json()
    created = await make_map(client)
    await client.put(
        f"/course-maps/{created['id']}/appearance",
        json={"settings": {}, "defaultSkinId": skin["id"]},
    )
    await publish(client, created["id"])

    await client.patch(f"/skins/{skin['id']}", json={"name": "Editada"})
    assert (await read(client, created["id"]))["skins"][skin["id"]][
        "name"
    ] == "Original"
    await client.delete(f"/skins/{skin['id']}")
    body = await read(client, created["id"])
    assert body["skins"][skin["id"]]["name"] == "Original"
    assert body["defaultSkinId"] == skin["id"]  # the published map still has it


# --- live data ------------------------------------------------------------------------


async def test_status_is_joined_live_and_a_vanished_bubble_reads_no_complete(
    client: httpx.AsyncClient,
) -> None:
    created, bubbles = await published_map(client, activities=(28, 29, 30))
    base = f"/course-maps/{created['id']}/bubbles"
    await client.patch(f"{base}/{bubbles[0]['id']}", json={"status": "complete"})
    await client.patch(f"{base}/{bubbles[1]['id']}", json={"status": "in_progress"})

    body = await read(client, created["id"])
    assert [b["status"] for b in body["bubbles"]] == [
        "complete",
        "in_progress",
        "locked",
    ]

    # The draft deletes a completed bubble: it is still published, and not done.
    await client.delete(f"{base}/{bubbles[0]['id']}")
    body = await read(client, created["id"])
    assert [b["id"] for b in body["bubbles"]] == [b["id"] for b in bubbles]
    assert [b["status"] for b in body["bubbles"]] == [
        "no_complete",
        "in_progress",
        "locked",
    ]
    [item] = (await client.get("/published/course-maps")).json()["items"]
    assert (item["bubbleCount"], item["completeCount"]) == (3, 0)


async def test_position_is_live(client: httpx.AsyncClient) -> None:
    first, _ = await published_map(client, activities=(28,))
    second, _ = await published_map(client, activities=(29,))
    assert (await read(client, second["id"]))["position"] == 1

    await client.put(
        "/course-maps/order",
        json={"moodleCourseId": 8, "mapIds": [second["id"], first["id"]]},
    )

    assert (await read(client, second["id"]))["position"] == 0


# --- resolved ---------------------------------------------------------------------------


async def test_resolved_uses_the_published_bubbles_even_after_the_draft_removed_one(
    client: httpx.AsyncClient,
) -> None:
    created, bubbles = await published_map(client, activities=(28, 9999))
    await client.delete(f"/course-maps/{created['id']}/bubbles/{bubbles[0]['id']}")

    published = await client.get(f"/published/course-maps/{created['id']}/resolved")
    draft = await client.get(f"/course-maps/{created['id']}/resolved")

    assert published.status_code == 200
    assert [b["bubbleId"] for b in published.json()["bubbles"]] == [
        b["id"] for b in bubbles
    ]
    assert [b["availability"] for b in published.json()["bubbles"]] == [
        "available",
        "missing",
    ]
    assert published.json()["bubbles"][0]["activity"]["activityId"] == 28
    assert published.json()["moodleStatus"] == "live"
    # The draft no longer has the first bubble.
    assert [b["bubbleId"] for b in draft.json()["bubbles"]] == [bubbles[1]["id"]]


async def test_resolved_applies_the_same_hidden_rules_as_the_editor(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    moodle.courses[8] = course8()  # section hidden for learners
    created, _ = await published_map(client, activities=(28,))
    url = f"/published/course-maps/{created['id']}/resolved"

    hidden = (await client.get(url)).json()["bubbles"][0]
    revealed = (await client.get(url, params={"includeHidden": "true"})).json()[
        "bubbles"
    ][0]
    editor = (await client.get(f"/course-maps/{created['id']}/resolved")).json()[
        "bubbles"
    ][0]

    assert hidden["availability"] == "hidden" and hidden["activity"] is None
    assert revealed["activity"]["activityId"] == 28
    assert hidden == editor


async def test_resolved_shares_the_moodle_cache_with_the_editor(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    created, _ = await published_map(client, activities=(28,))
    moodle.calls = 0

    await client.get(f"/course-maps/{created['id']}/resolved")
    await client.get(f"/published/course-maps/{created['id']}/resolved")
    await client.get(f"/published/course-maps/{created['id']}/resolved")

    assert moodle.calls == 1


async def test_resolved_degrades_when_moodle_is_down(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    from app.moodle.exceptions import MoodleUnavailableError

    created, _ = await published_map(client, activities=(28,))
    moodle.error = MoodleUnavailableError("down")

    r = await client.get(f"/published/course-maps/{created['id']}/resolved")

    assert r.status_code == 200
    assert r.json()["moodleStatus"] == "unavailable"
    assert r.json()["bubbles"][0]["availability"] == "unknown"


async def test_restoring_into_the_draft_does_not_touch_what_students_see(
    client: httpx.AsyncClient,
) -> None:
    created, _ = await published_map(client)
    await client.patch(f"/course-maps/{created['id']}", json={"title": "v2"})
    await publish(client, created["id"])
    before = await read(client, created["id"])
    revision = (await full_map(client, created["id"]))["revision"]

    r = await client.post(
        f"/course-maps/{created['id']}/publications/1/restore",
        headers={"If-Match": f'"{revision}"'},
    )

    assert r.status_code == 200 and r.json()["map"]["title"] == "Mapa"
    assert await read(client, created["id"]) == before
