"""`GET /course-maps/{id}/activities` through the real stack, with a fake Moodle."""

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from app.moodle.dependencies import get_moodle_client
from app.moodle.exceptions import (
    MoodleAuthError,
    MoodleCourseNotFoundError,
    MoodleError,
    MoodleUnavailableError,
)
from app.moodle.schemas import MoodleModule, MoodleSection
from tests.fakes import InMemoryMoodleClient


@pytest.fixture
def moodle() -> Iterator[InMemoryMoodleClient]:
    fake = InMemoryMoodleClient()
    app.dependency_overrides[get_moodle_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_moodle_client, None)


@pytest.fixture
async def client(
    engine: AsyncEngine, moodle: InMemoryMoodleClient
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def _create_map(client: httpx.AsyncClient, course_id: int) -> str:
    r = await client.post(
        "/course-maps", json={"moodleCourseId": course_id, "imageUrl": "http://i/x"}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_returns_camel_case_activities_with_placement(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _create_map(client, 9)
    r = await client.post(
        f"/course-maps/{map_id}/bubbles", json={"activityId": 101, "x": 0.1, "y": 0.2}
    )
    bubble_id = r.json()["id"]
    moodle.courses[9] = [
        MoodleSection(
            id=1,
            name="Unit 1",
            modules=[
                MoodleModule(id=101, name="Quiz 1", modname="quiz"),
                MoodleModule(id=102, name="Reading", modname="resource"),
                MoodleModule(id=103, name="Text", modname="label"),
            ],
        )
    ]

    r = await client.get(f"/course-maps/{map_id}/activities")

    assert r.status_code == 200
    assert r.json() == [
        {
            "activityId": 101,
            "name": "Quiz 1",
            "modname": "quiz",
            "sectionName": "Unit 1",
            "placed": True,
            "bubbleId": bubble_id,
        },
        {
            "activityId": 102,
            "name": "Reading",
            "modname": "resource",
            "sectionName": "Unit 1",
            "placed": False,
            "bubbleId": None,
        },
    ]


async def test_unknown_map_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/course-maps/{uuid.uuid4()}/activities")
    assert r.status_code == 404
    assert r.json() == {"detail": "Course map not found"}


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (MoodleCourseNotFoundError("x"), 404, "Moodle course not found"),
        (MoodleAuthError("x"), 502, "Moodle integration error"),
        (MoodleError("x"), 502, "Moodle integration error"),
        (MoodleUnavailableError("x"), 503, "Moodle is unavailable"),
    ],
)
async def test_moodle_errors_map_to_http_status(
    client: httpx.AsyncClient,
    moodle: InMemoryMoodleClient,
    error: MoodleError,
    status_code: int,
    detail: str,
) -> None:
    map_id = await _create_map(client, 9)
    moodle.error = error

    r = await client.get(f"/course-maps/{map_id}/activities")

    assert r.status_code == status_code
    assert r.json() == {"detail": detail}


async def test_error_response_does_not_leak_exception_text(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _create_map(client, 9)
    moodle.error = MoodleAuthError("token=abc123 errorcode=invalidtoken")

    r = await client.get(f"/course-maps/{map_id}/activities")

    assert "abc123" not in r.text and "invalidtoken" not in r.text
    assert r.json() == {"detail": "Moodle integration error"}
