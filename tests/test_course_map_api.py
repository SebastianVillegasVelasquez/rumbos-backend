"""API tests through the real stack (routes -> service -> repositories -> Postgres)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine  # the test database, instead of the lifespan engine
    app.state.moodle_http = httpx.AsyncClient()  # never called by these tests
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


async def test_create_course_map_conflict_for_same_course(
    client: httpx.AsyncClient,
) -> None:
    await _create_map(client, 5)
    r = await client.post(
        "/course-maps",
        json={"title": "Other", "moodleCourseId": 5, "imageUrl": "/other"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "map_already_exists_for_course"


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
        "moodleCourseId",
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
