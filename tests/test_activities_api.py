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
from tests.fakes import InMemoryMoodleClient
from tests.moodle_fixtures import COURSE8_MODULE_ORDER, course8, parse, raw_course8


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
        "/course-maps",
        json={
            "title": f"Map {course_id}",
            "moodleCourseId": course_id,
            "imageUrl": "http://i/x",
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_course8_defaults_to_no_activities(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    """The real course has one hidden section: zero activities is correct."""
    map_id = await _create_map(client, 8)
    moodle.courses[8] = course8()

    r = await client.get(f"/course-maps/{map_id}/activities")

    assert r.status_code == 200
    assert r.json() == []


async def test_include_hidden_returns_camel_case_activities(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _create_map(client, 8)
    r = await client.post(
        f"/course-maps/{map_id}/bubbles", json={"activityId": 25, "x": 0.1, "y": 0.2}
    )
    bubble_id = r.json()["id"]
    moodle.courses[8] = course8()

    r = await client.get(
        f"/course-maps/{map_id}/activities", params={"includeHidden": "true"}
    )

    assert r.status_code == 200
    body = r.json()
    assert [a["activityId"] for a in body] == COURSE8_MODULE_ORDER
    assert body[0] == {
        "activityId": 28,
        "name": "Preguntas para reconocer cuánto sabes [IN-1]",
        "modname": "quiz",
        "url": "https://academiaturismo.mincit.gov.co/mod/quiz/view.php?id=28",
        "sectionName": "Recursos",
        "sectionNumber": 1,
        "hidden": True,
        "placed": False,
        "bubbleId": None,
    }
    quiz_25 = next(a for a in body if a["activityId"] == 25)
    assert quiz_25["placed"] is True and quiz_25["bubbleId"] == bubble_id
    assert quiz_25["name"].endswith("[OUT-2]")


async def test_include_hidden_false_explicitly_excludes_hidden(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _create_map(client, 8)
    moodle.courses[8] = course8()

    r = await client.get(
        f"/course-maps/{map_id}/activities", params={"includeHidden": "false"}
    )

    assert r.json() == []


async def test_visible_section_activities_are_not_hidden(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    map_id = await _create_map(client, 8)
    moodle.courses[8] = parse(raw)

    r = await client.get(f"/course-maps/{map_id}/activities")

    body = r.json()
    assert [a["activityId"] for a in body] == COURSE8_MODULE_ORDER
    assert not any(a["hidden"] for a in body)


async def test_hostile_section_summary_never_reaches_the_response(
    client: httpx.AsyncClient, moodle: InMemoryMoodleClient
) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    marker = "EVIL_MARKER"
    raw[1]["summary"] = f"<script>alert('{marker}')</script>" + "x" * 200_000
    raw[0]["summary"] = raw[1]["summary"]
    map_id = await _create_map(client, 8)
    moodle.courses[8] = parse(raw)

    r = await client.get(
        f"/course-maps/{map_id}/activities", params={"includeHidden": "true"}
    )

    assert r.status_code == 200
    assert marker not in r.text and "<script>" not in r.text
    assert "summary" not in r.text
    assert len(r.content) < 10_000


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
