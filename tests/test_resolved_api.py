"""`GET /course-maps/{id}/resolved` through the real stack.

Routes -> services -> Postgres, with the real `CachedMoodleClient` over a fake
Moodle and a fake clock, wired the way the lifespan wires it.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from app.moodle.cache import CachedMoodleClient
from app.moodle.exceptions import MoodleAuthError, MoodleUnavailableError
from tests.fakes import FakeClock, InMemoryMoodleClient
from tests.moodle_fixtures import course8, parse, raw_course8

TTL = 60.0
STALE_MAX = 3600.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def moodle() -> InMemoryMoodleClient:
    fake = InMemoryMoodleClient()
    fake.courses[8] = course8()
    return fake


@pytest.fixture
async def client(
    engine: AsyncEngine, moodle: InMemoryMoodleClient, clock: FakeClock
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = CachedMoodleClient(
        moodle, ttl_seconds=TTL, stale_max_seconds=STALE_MAX, clock=clock
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _map_with_bubbles(
    client: httpx.AsyncClient, activity_ids: list[int], course_id: int = 8
) -> tuple[str, list[str]]:
    r = await client.post(
        "/course-maps",
        json={"title": "Ruta", "moodleCourseId": course_id, "imageUrl": "/m.png"},
    )
    assert r.status_code == 201, r.text
    map_id = r.json()["id"]
    bubble_ids = []
    for activity_id in activity_ids:
        b = await client.post(
            f"/course-maps/{map_id}/bubbles",
            json={"activityId": activity_id, "x": 0.5, "y": 0.5},
        )
        assert b.status_code == 201, b.text
        bubble_ids.append(b.json()["id"])
    return str(map_id), bubble_ids


def _visible(moodle: InMemoryMoodleClient) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    moodle.courses[8] = parse(raw)


async def test_resolved_contract_is_camel_case_and_complete(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    _visible(moodle)
    map_id, [b1, b2] = await _map_with_bubbles(client, [28, 9999])

    r = await client.get(f"/course-maps/{map_id}/resolved")

    assert r.status_code == 200
    assert r.json() == {
        "moodleStatus": "live",
        "bubbles": [
            {
                "bubbleId": b1,
                "availability": "available",
                "activity": {
                    "activityId": 28,
                    "name": "Preguntas para reconocer cuánto sabes [IN-1]",
                    "modname": "quiz",
                    "sectionName": "Recursos",
                    "sectionNumber": 1,
                    "url": "https://academiaturismo.mincit.gov.co/mod/quiz/view.php?id=28",
                },
            },
            {"bubbleId": b2, "availability": "missing", "activity": None},
        ],
    }


async def test_hidden_activity_is_only_returned_with_include_hidden(
    client: httpx.AsyncClient,
) -> None:
    map_id, [bubble_id] = await _map_with_bubbles(client, [28])

    default = (await client.get(f"/course-maps/{map_id}/resolved")).json()
    explicit = (
        await client.get(
            f"/course-maps/{map_id}/resolved", params={"includeHidden": "false"}
        )
    ).json()
    revealed = (
        await client.get(
            f"/course-maps/{map_id}/resolved", params={"includeHidden": "true"}
        )
    ).json()

    for body in (default, explicit):
        assert body["bubbles"] == [
            {"bubbleId": bubble_id, "availability": "hidden", "activity": None}
        ]
        assert "IN-1" not in str(body)
    entry = revealed["bubbles"][0]
    assert entry["availability"] == "hidden"
    assert entry["activity"]["activityId"] == 28


async def test_unknown_map_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/course-maps/{uuid.uuid4()}/resolved")
    assert r.status_code == 404
    assert r.json() == {"detail": "Course map not found"}


async def test_moodle_down_without_cache_is_200_unavailable(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    map_id, [b1, b2] = await _map_with_bubbles(client, [28, 25])
    moodle.error = MoodleUnavailableError("down")

    r = await client.get(f"/course-maps/{map_id}/resolved")

    assert r.status_code == 200
    assert r.json() == {
        "moodleStatus": "unavailable",
        "bubbles": [
            {"bubbleId": b1, "availability": "unknown", "activity": None},
            {"bubbleId": b2, "availability": "unknown", "activity": None},
        ],
    }


async def test_moodle_down_with_cache_is_200_cached(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient, clock: FakeClock
) -> None:
    _visible(moodle)
    map_id, _ = await _map_with_bubbles(client, [28])
    first = (await client.get(f"/course-maps/{map_id}/resolved")).json()
    clock.advance(TTL + 1)
    moodle.error = MoodleUnavailableError("down")

    r = await client.get(f"/course-maps/{map_id}/resolved")

    assert r.status_code == 200
    body = r.json()
    assert first["moodleStatus"] == "live" and body["moodleStatus"] == "cached"
    assert body["bubbles"] == first["bubbles"]


async def test_auth_error_is_200_unavailable_and_logged_at_error(
    client: httpx.AsyncClient,
    moodle: InMemoryMoodleClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    map_id, _ = await _map_with_bubbles(client, [28])
    moodle.error = MoodleAuthError("token=SECRET123 errorcode=invalidtoken")

    with caplog.at_level(logging.WARNING):
        r = await client.get(f"/course-maps/{map_id}/resolved")

    assert r.status_code == 200
    assert r.json()["moodleStatus"] == "unavailable"
    assert r.json()["bubbles"][0]["availability"] == "unknown"
    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(errors) == 1 and "MoodleAuthError" in errors[0].getMessage()
    assert "SECRET123" not in caplog.text and "invalidtoken" not in caplog.text
    assert "SECRET123" not in r.text


async def test_resolved_and_activities_share_one_cache(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    map_id, _ = await _map_with_bubbles(client, [28])

    await client.get(f"/course-maps/{map_id}/resolved")
    r = await client.get(
        f"/course-maps/{map_id}/activities", params={"includeHidden": "true"}
    )

    assert r.status_code == 200
    assert moodle.calls == 1


async def test_activities_serves_stale_data_when_moodle_is_down(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient, clock: FakeClock
) -> None:
    map_id, _ = await _map_with_bubbles(client, [28])
    params = {"includeHidden": "true"}
    fresh = (
        await client.get(f"/course-maps/{map_id}/activities", params=params)
    ).json()
    clock.advance(TTL + 1)
    moodle.error = MoodleUnavailableError("down")

    r = await client.get(f"/course-maps/{map_id}/activities", params=params)

    assert r.status_code == 200 and r.json() == fresh
    # Past the stale limit the existing error mapping applies again.
    clock.advance(STALE_MAX)
    gone = await client.get(f"/course-maps/{map_id}/activities", params=params)
    assert gone.status_code == 503
    assert gone.json() == {"detail": "Moodle is unavailable"}


async def test_get_course_map_never_calls_moodle(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    map_id, _ = await _map_with_bubbles(client, [28])
    moodle.error = MoodleUnavailableError("down")

    detail = await client.get(f"/course-maps/{map_id}")
    listing = await client.get("/course-maps")

    assert detail.status_code == 200 and listing.status_code == 200
    assert moodle.calls == 0


async def test_burst_of_resolved_requests_costs_one_moodle_call(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    map_id, _ = await _map_with_bubbles(client, [28])

    responses: list[httpx.Response] = await asyncio.gather(
        *(client.get(f"/course-maps/{map_id}/resolved") for _ in range(15))
    )

    assert all(r.status_code == 200 for r in responses)
    assert moodle.calls == 1


@pytest.mark.parametrize("value", ["maybe", "2"])
async def test_include_hidden_must_be_a_boolean(
    client: httpx.AsyncClient, value: Any
) -> None:
    map_id, _ = await _map_with_bubbles(client, [28])
    r = await client.get(
        f"/course-maps/{map_id}/resolved", params={"includeHidden": value}
    )
    assert r.status_code == 422
