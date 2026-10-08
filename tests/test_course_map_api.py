"""API tests through the real stack (routes -> service -> repositories -> Postgres)."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from tests import skin_configs
from tests.fakes import InMemoryMoodleClient


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine  # the test database, instead of the lifespan engine
    app.state.moodle_client = InMemoryMoodleClient()  # never called by these tests
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _create_map(client: httpx.AsyncClient, course_id: int = 1) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={
            "title": f"Map {course_id}",
            "moodleCourseId": course_id,
            "imageUrl": "http://i/x",
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def _add_bubble(
    client: httpx.AsyncClient, map_id: str, **overrides: Any
) -> dict[str, Any]:
    payload = {"activityId": 1, "x": 0.25, "y": 0.75, **overrides}
    r = await client.post(f"/course-maps/{map_id}/bubbles", json=payload)
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_create_course_map(client: httpx.AsyncClient) -> None:
    body = await _create_map(client, 42)
    assert body["moodleCourseId"] == 42
    assert uuid.UUID(body["id"]).version == 7


async def test_a_course_can_have_several_maps_appended_in_order(
    client: httpx.AsyncClient,
) -> None:
    first = await _create_map(client, 5)
    second = await _create_map(client, 5)
    other_course = await _create_map(client, 6)

    assert [first["position"], second["position"], other_course["position"]] == [
        0,
        1,
        0,
    ]
    assert first["moodleSectionId"] is None


async def test_create_course_map_conflict_for_same_section(
    client: httpx.AsyncClient,
) -> None:
    payload = {
        "title": "L1",
        "moodleCourseId": 5,
        "imageUrl": "/a",
        "moodleSectionId": 31,
    }
    created = await client.post("/course-maps", json=payload)
    assert created.status_code == 201
    assert created.json()["moodleSectionId"] == 31

    r = await client.post("/course-maps", json=payload)

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "map_already_exists_for_section"
    # Another course may use the same section id; maps without one never collide.
    other = await client.post("/course-maps", json={**payload, "moodleCourseId": 6})
    assert other.status_code == 201
    assert (await _create_map(client, 5))["moodleSectionId"] is None


async def test_create_course_map_returns_title_and_empty_bubbles(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/course-maps",
        json={"title": "  Ruta del café ", "moodleCourseId": 3, "imageUrl": "/m.png"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Ruta del café"
    assert body["bubbles"] == []
    assert body["imageUrl"] == "/m.png"


@pytest.mark.parametrize(
    "payload",
    [
        {"moodleCourseId": 1, "imageUrl": "/a"},
        {"title": "", "moodleCourseId": 1, "imageUrl": "/a"},
        {"title": "x" * 121, "moodleCourseId": 1, "imageUrl": "/a"},
        {"title": "T", "moodleCourseId": 1, "imageUrl": "javascript:alert(1)"},
        {"title": "T", "moodleCourseId": 1, "imageUrl": "data:text/html,x"},
    ],
)
async def test_create_course_map_rejects_invalid_payloads(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    assert (await client.post("/course-maps", json=payload)).status_code == 422


async def test_create_course_map_validation_error(client: httpx.AsyncClient) -> None:
    r = await client.post("/course-maps", json={"moodleCourseId": "abc"})
    assert r.status_code == 422


async def test_get_course_map_with_bubbles(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    await _add_bubble(client, created["id"], activityId=11)
    await _add_bubble(client, created["id"], activityId=12, icon="chest")

    r = await client.get(f"/course-maps/{created['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == created["id"]
    assert [b["activityId"] for b in body["bubbles"]] == [11, 12]
    assert body["bubbles"][1]["icon"] == "chest"


async def test_get_missing_course_map_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/course-maps/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_add_bubble_defaults_and_validation(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    bubble = await _add_bubble(client, created["id"])
    assert bubble["status"] == "locked" and bubble["icon"] is None

    bad = await client.post(
        f"/course-maps/{created['id']}/bubbles",
        json={"activityId": 1, "x": 1.5, "y": 0.5},
    )
    assert bad.status_code == 422
    missing = await client.post(
        f"/course-maps/{uuid.uuid4()}/bubbles", json={"activityId": 1, "x": 0, "y": 0}
    )
    assert missing.status_code == 404


async def test_add_bubble_for_a_placed_activity_is_409_with_code(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    await _add_bubble(client, created["id"], activityId=5)

    r = await client.post(
        f"/course-maps/{created['id']}/bubbles",
        json={"activityId": 5, "x": 0.1, "y": 0.1},
    )

    assert r.status_code == 409
    assert r.json() == {
        "detail": {
            "code": "activity_already_placed",
            "message": "This activity already has a bubble on a map of the course",
            "courseMapId": created["id"],
            "courseMapTitle": "Map 1",
        }
    }
    other_course = await _create_map(client, 2)
    await _add_bubble(client, other_course["id"], activityId=5)  # other course: fine


async def test_activity_is_unique_across_the_maps_of_a_course(
    client: httpx.AsyncClient,
) -> None:
    first = await _create_map(client, 1)
    second = await _create_map(client, 1)
    await _add_bubble(client, first["id"], activityId=5)

    r = await client.post(
        f"/course-maps/{second['id']}/bubbles",
        json={"activityId": 5, "x": 0.1, "y": 0.1},
    )

    assert r.status_code == 409
    assert r.json()["detail"]["courseMapId"] == first["id"]
    # Deleting the map that holds it frees the activity again.
    assert (await client.delete(f"/course-maps/{first['id']}")).status_code == 204
    await _add_bubble(client, second["id"], activityId=5)


async def test_a_maps_course_can_never_change(client: httpx.AsyncClient) -> None:
    created = await _create_map(client, 4)
    await _add_bubble(client, created["id"])

    r = await client.patch(
        f"/course-maps/{created['id']}",
        json={"title": "Renamed", "moodleCourseId": 99, "moodleSectionId": 7},
    )

    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"
    assert r.json()["moodleCourseId"] == 4  # silently not updatable
    assert r.json()["moodleSectionId"] is None
    only_course = await client.patch(
        f"/course-maps/{created['id']}", json={"moodleCourseId": 99}
    )
    assert only_course.status_code == 422  # nothing updatable was sent


async def test_reorder_course_maps(client: httpx.AsyncClient) -> None:
    a = await _create_map(client, 3)
    b = await _create_map(client, 3)
    c = await _create_map(client, 3)
    await _add_bubble(client, b["id"], activityId=1, status="complete")
    await _add_bubble(client, b["id"], activityId=2)
    other = await _create_map(client, 4)

    r = await client.put(
        "/course-maps/order",
        json={"moodleCourseId": 3, "mapIds": [c["id"], a["id"], b["id"]]},
    )

    assert r.status_code == 200
    items = r.json()["items"]
    assert [m["id"] for m in items] == [c["id"], a["id"], b["id"]]
    assert [m["position"] for m in items] == [0, 1, 2]
    assert [(m["bubbleCount"], m["completeCount"]) for m in items] == [
        (0, 0),
        (0, 0),
        (2, 1),
    ]
    listed = await client.get("/course-maps", params={"moodleCourseId": 3})
    assert [m["id"] for m in listed.json()["items"]] == [c["id"], a["id"], b["id"]]
    untouched = await client.get(f"/course-maps/{other['id']}")
    assert untouched.json()["position"] == 0


@pytest.mark.parametrize("case", ["missing", "unknown", "duplicate", "foreign"])
async def test_reorder_requires_exactly_the_courses_maps(
    client: httpx.AsyncClient, case: str
) -> None:
    a, b = await _create_map(client, 3), await _create_map(client, 3)
    foreign = await _create_map(client, 4)
    ids = {
        "missing": [a["id"]],
        "unknown": [a["id"], b["id"], str(uuid.uuid4())],
        "duplicate": [a["id"], a["id"], b["id"]],
        "foreign": [a["id"], b["id"], foreign["id"]],
    }[case]

    r = await client.put(
        "/course-maps/order", json={"moodleCourseId": 3, "mapIds": ids}
    )

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "order_mismatch"
    after = await client.get("/course-maps", params={"moodleCourseId": 3})
    assert [m["id"] for m in after.json()["items"]] == [a["id"], b["id"]]


async def test_concurrent_add_bubble_requests_create_one(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)

    async def post() -> int:
        r = await client.post(
            f"/course-maps/{created['id']}/bubbles",
            json={"activityId": 9, "x": 0.3, "y": 0.3},
        )
        return r.status_code

    codes = await asyncio.gather(*(post() for _ in range(6)))

    assert sorted(codes) == [201] + [409] * 5
    body = (await client.get(f"/course-maps/{created['id']}")).json()
    assert len(body["bubbles"]) == 1


async def test_patch_bubble_is_partial(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    bubble = await _add_bubble(client, created["id"], icon="star")
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    moved = await client.patch(url, json={"x": 0.9, "y": 0.1})
    assert moved.status_code == 200
    assert (moved.json()["x"], moved.json()["y"]) == (0.9, 0.1)
    assert moved.json()["icon"] == "star"  # not resent, not changed

    status_only = await client.patch(url, json={"status": "complete"})
    assert status_only.json()["status"] == "complete"
    assert (status_only.json()["x"], status_only.json()["y"]) == (0.9, 0.1)

    cleared = await client.patch(url, json={"icon": None})
    assert cleared.json()["icon"] is None


@pytest.mark.parametrize(
    "payload", [{}, {"x": 0.5}, {"status": "done"}, {"icon": "rocket"}]
)
async def test_patch_bubble_rejects_invalid_payloads(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    created = await _create_map(client)
    bubble = await _add_bubble(client, created["id"])
    r = await client.patch(
        f"/course-maps/{created['id']}/bubbles/{bubble['id']}", json=payload
    )
    assert r.status_code == 422


async def test_patch_missing_or_foreign_bubble_is_404(
    client: httpx.AsyncClient,
) -> None:
    owner = await _create_map(client, 1)
    other = await _create_map(client, 2)
    bubble = await _add_bubble(client, owner["id"])

    wrong_map = await client.patch(
        f"/course-maps/{other['id']}/bubbles/{bubble['id']}", json={"status": "locked"}
    )
    missing = await client.patch(
        f"/course-maps/{owner['id']}/bubbles/{uuid.uuid4()}", json={"status": "locked"}
    )
    assert wrong_map.status_code == 404 and missing.status_code == 404


async def test_delete_bubble(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    bubble = await _add_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    assert (await client.delete(url)).status_code == 204
    assert (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"] == []
    assert (await client.delete(url)).status_code == 404


async def test_responses_are_camel_case(client: httpx.AsyncClient) -> None:
    created = await _create_map(client, 7)
    assert set(created) == {
        "id",
        "title",
        "bubbles",
        "settings",
        "defaultSkinId",
        "skinRules",
        "moodleCourseId",
        "moodleSectionId",
        "position",
        "imageUrl",
        "createdAt",
        "updatedAt",
    }
    bubble = await _add_bubble(client, created["id"])
    assert set(bubble) == {
        "id",
        "courseMapId",
        "activityId",
        "x",
        "y",
        "icon",
        "status",
        "skinId",
        "sequence",
        "createdAt",
        "updatedAt",
    }
    detail = (await client.get(f"/course-maps/{created['id']}")).json()
    assert set(detail["bubbles"][0]) == set(bubble)


async def test_snake_case_request_bodies_are_still_accepted(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post(
        "/course-maps",
        json={"title": "T", "moodle_course_id": 9, "image_url": "http://i/y"},
    )
    assert r.status_code == 201
    assert r.json()["moodleCourseId"] == 9  # but the response is always camelCase


async def test_camel_case_request_body_is_applied(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    r = await client.post(
        f"/course-maps/{created['id']}/bubbles",
        json={"activityId": 77, "x": 0.5, "y": 0.5, "status": "in_progress"},
    )
    assert r.status_code == 201
    assert r.json()["activityId"] == 77
    assert r.json()["courseMapId"] == created["id"]


async def test_list_course_maps_contract(client: httpx.AsyncClient) -> None:
    first = await _create_map(client, 1)
    second = await _create_map(client, 2)
    await _add_bubble(client, first["id"], activityId=1)
    await _add_bubble(client, first["id"], activityId=2)

    r = await client.get("/course-maps")

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert (body["total"], body["limit"], body["offset"]) == (2, 24, 0)
    # Most recently updated first, and summaries never carry bubbles.
    assert [m["id"] for m in body["items"]] == [second["id"], first["id"]]
    assert set(body["items"][0]) == {
        "id",
        "title",
        "moodleCourseId",
        "moodleSectionId",
        "position",
        "imageUrl",
        "bubbleCount",
        "completeCount",
        "createdAt",
        "updatedAt",
    }
    assert [m["bubbleCount"] for m in body["items"]] == [0, 2]


async def test_list_course_maps_filters_and_pages(client: httpx.AsyncClient) -> None:
    for course_id, title in [(1, "Ruta Café"), (2, "Ruta Mar"), (3, "Otro")]:
        r = await client.post(
            "/course-maps",
            json={"title": title, "moodleCourseId": course_id, "imageUrl": "/a"},
        )
        assert r.status_code == 201

    by_q = (await client.get("/course-maps", params={"q": "ruta"})).json()
    assert by_q["total"] == 2

    by_course = (await client.get("/course-maps", params={"moodleCourseId": 3})).json()
    assert [m["title"] for m in by_course["items"]] == ["Otro"]

    paged = (await client.get("/course-maps", params={"limit": 1, "offset": 1})).json()
    assert paged["total"] == 3 and len(paged["items"]) == 1
    assert (paged["limit"], paged["offset"]) == (1, 1)


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"moodleCourseId": "abc"}],
)
async def test_list_course_maps_rejects_bad_paging(
    client: httpx.AsyncClient, params: dict[str, Any]
) -> None:
    assert (await client.get("/course-maps", params=params)).status_code == 422


async def test_list_course_maps_accepts_max_limit(client: httpx.AsyncClient) -> None:
    r = await client.get("/course-maps", params={"limit": 100})
    assert r.status_code == 200 and r.json()["limit"] == 100


async def test_patch_course_map_is_partial(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    await _add_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}"

    renamed = await client.patch(url, json={"title": "  Nuevo nombre "})
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["title"] == "Nuevo nombre"
    assert body["imageUrl"] == created["imageUrl"]
    assert len(body["bubbles"]) == 1  # PATCH returns the full CourseMapRead
    assert body["updatedAt"] > created["updatedAt"]

    image = await client.patch(url, json={"imageUrl": "/new.png"})
    assert image.json()["imageUrl"] == "/new.png"
    assert image.json()["title"] == "Nuevo nombre"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": None},
        {"title": "   "},
        {"title": "x" * 121},
        {"imageUrl": "javascript:alert(1)"},
        {"imageUrl": "data:image/png;base64,AAAA"},
        {"imageUrl": None},
        {"moodleCourseId": 99},  # not editable, so on its own it is an empty patch
    ],
)
async def test_patch_course_map_rejects_invalid_payloads(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    created = await _create_map(client)
    r = await client.patch(f"/course-maps/{created['id']}", json=payload)
    assert r.status_code == 422
    unchanged = (await client.get(f"/course-maps/{created['id']}")).json()
    assert unchanged["title"] == created["title"]


async def test_patch_missing_course_map_is_404(client: httpx.AsyncClient) -> None:
    r = await client.patch(f"/course-maps/{uuid.uuid4()}", json={"title": "x"})
    assert r.status_code == 404


async def test_delete_course_map_removes_its_bubbles(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    await _add_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}"

    r = await client.delete(url)

    assert r.status_code == 204 and r.content == b""
    assert (await client.get(url)).status_code == 404
    assert (await client.get("/course-maps")).json()["total"] == 0
    assert (await client.delete(url)).status_code == 404
    # The course is free again for a new map.
    assert (await _create_map(client)).get("bubbles") == []


async def _create_skin(client: httpx.AsyncClient) -> str:
    r = await client.post(
        "/skins", json={"name": "S", "config": skin_configs.procedural()}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_new_bubbles_go_last_in_the_path(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    other = await _create_map(client, 2)

    sequences = [
        (await _add_bubble(client, created["id"], activityId=a))["sequence"]
        for a in (1, 2, 3)
    ]
    first_elsewhere = await _add_bubble(client, other["id"], activityId=9)

    assert sequences == [0, 1, 2]
    assert first_elsewhere["sequence"] == 0  # per map
    assert (await _add_bubble(client, created["id"], activityId=4))["sequence"] == 3
    assert all(b["skinId"] is None for b in [first_elsewhere])


async def test_patch_bubble_sets_and_clears_its_skin(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    bubble = await _add_bubble(client, created["id"])
    skin_id = await _create_skin(client)
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    set_skin = await client.patch(url, json={"skinId": skin_id})
    other_field = await client.patch(url, json={"x": 0.9, "y": 0.9})
    cleared = await client.patch(url, json={"skinId": None})

    assert set_skin.status_code == 200 and set_skin.json()["skinId"] == skin_id
    assert other_field.json()["skinId"] == skin_id  # untouched when not sent
    assert cleared.json()["skinId"] is None
    stored = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"][0]
    assert stored["skinId"] is None


async def test_patch_bubble_with_an_unknown_skin_is_422_and_changes_nothing(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    bubble = await _add_bubble(client, created["id"])
    ghost = str(uuid.uuid4())

    r = await client.patch(
        f"/course-maps/{created['id']}/bubbles/{bubble['id']}",
        json={"skinId": ghost, "x": 0.9, "y": 0.9},
    )

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "skin_not_found"
    assert r.json()["detail"]["skinIds"] == [ghost]
    stored = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"][0]
    assert (stored["x"], stored["skinId"]) == (0.25, None)


async def test_sequence_cannot_be_patched(client: httpx.AsyncClient) -> None:
    created = await _create_map(client)
    await _add_bubble(client, created["id"], activityId=1)
    second = await _add_bubble(client, created["id"], activityId=2)
    url = f"/course-maps/{created['id']}/bubbles/{second['id']}"

    only_sequence = await client.patch(url, json={"sequence": 0})
    with_other = await client.patch(url, json={"sequence": 0, "status": "complete"})

    assert only_sequence.status_code == 422  # nothing patchable was sent
    assert with_other.status_code == 200 and with_other.json()["sequence"] == 1


async def test_a_deleted_skin_leaves_bubbles_with_no_skin_through_the_api(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_map(client)
    bubble = await _add_bubble(client, created["id"])
    skin_id = await _create_skin(client)
    await client.patch(
        f"/course-maps/{created['id']}/bubbles/{bubble['id']}",
        json={"skinId": skin_id},
    )

    assert (await client.delete(f"/skins/{skin_id}")).status_code == 204

    stored = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"][0]
    assert stored["skinId"] is None


async def test_reordering_a_course_without_maps_is_an_empty_success(
    client: httpx.AsyncClient,
) -> None:
    r = await client.put(
        "/course-maps/order", json={"moodleCourseId": 77, "mapIds": []}
    )

    assert r.status_code == 200
    assert r.json() == {"items": []}
