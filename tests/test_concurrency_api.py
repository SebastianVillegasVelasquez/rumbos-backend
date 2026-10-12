"""Optimistic concurrency: `If-Match` / `ETag` on revision and version."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
from tests.api_client import ApiClient
from tests.fakes import InMemoryMoodleClient


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """A plain client: it sends no `If-Match` unless a test does."""
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with ApiClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


def match(number: int) -> dict[str, str]:
    return {"If-Match": f'"{number}"'}


async def make_map(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={"title": "T", "moodleCourseId": 1, "imageUrl": "http://i/x"},
    )
    assert r.status_code == 201
    body: dict[str, Any] = r.json()
    return body


async def make_bubble(
    client: httpx.AsyncClient, map_id: str, activity: int = 1
) -> dict[str, Any]:
    r = await client.post(
        f"/course-maps/{map_id}/bubbles",
        json={"activityId": activity, "x": 0.1, "y": 0.2},
    )
    assert r.status_code == 201
    body: dict[str, Any] = r.json()
    return body


APPEARANCE: dict[str, Any] = {"settings": {"mode": "guided"}}

# --- starting values and ETag ------------------------------------------------


async def test_revision_and_version_start_at_one_and_travel_as_etags(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/course-maps",
        json={"title": "T", "moodleCourseId": 1, "imageUrl": "http://i/x"},
    )
    assert created.json()["revision"] == 1
    assert created.headers["etag"] == '"1"'
    map_id = created.json()["id"]

    bubble = await client.post(
        f"/course-maps/{map_id}/bubbles", json={"activityId": 1, "x": 0, "y": 0}
    )
    assert bubble.json()["version"] == 1
    assert bubble.headers["etag"] == '"1"'

    got = await client.get(f"/course-maps/{map_id}")
    assert got.headers["etag"] == '"1"'
    assert got.json()["bubbles"][0]["version"] == 1


# --- 428 / 400 ----------------------------------------------------------------


async def test_guarded_writes_without_if_match_are_428(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    base = f"/course-maps/{created['id']}"

    responses = [
        await client.patch(base, json={"title": "x"}),
        await client.put(f"{base}/appearance", json=APPEARANCE),
        await client.patch(f"{base}/bubbles/{bubble['id']}", json={"x": 0.5, "y": 0.5}),
        await client.patch(f"{base}/bubbles/{bubble['id']}", json={"icon": "star"}),
        await client.patch(f"{base}/bubbles/{bubble['id']}", json={"skinId": None}),
        await client.delete(f"{base}/bubbles/{bubble['id']}"),
    ]

    for r in responses:
        assert r.status_code == 428, r.request.url
        assert r.json()["detail"]["code"] == "precondition_required"
    # Nothing was applied.
    after = (await client.get(base)).json()
    assert after["title"] == "T" and after["revision"] == 1
    assert after["bubbles"][0]["x"] == 0.1 and after["bubbles"][0]["version"] == 1


@pytest.mark.parametrize(
    "value",
    ["1", 'W/"1"', '"abc"', '"1", "2"', "*", '""', '"-1"', '"1.5"', '"1234567890"'],
)
async def test_a_malformed_if_match_is_400(
    client: httpx.AsyncClient, value: str
) -> None:
    created = await make_map(client)

    r = await client.patch(
        f"/course-maps/{created['id']}",
        json={"title": "x"},
        headers={"If-Match": value},
    )

    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_if_match"


# --- 412 -------------------------------------------------------------------------


async def test_a_stale_map_write_is_412_with_the_current_revision(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    base = f"/course-maps/{created['id']}"
    first = await client.patch(base, json={"title": "uno"}, headers=match(1))
    assert first.status_code == 200 and first.json()["revision"] == 2

    stale_patch = await client.patch(base, json={"title": "dos"}, headers=match(1))
    stale_put = await client.put(
        f"{base}/appearance", json=APPEARANCE, headers=match(1)
    )

    for r in (stale_patch, stale_put):
        assert r.status_code == 412
        detail = r.json()["detail"]
        assert detail["code"] == "version_conflict"
        assert detail["currentRevision"] == 2
        assert "currentVersion" not in detail
        assert r.headers["etag"] == '"2"'
    after = (await client.get(base)).json()
    assert after["title"] == "uno" and after["settings"]["mode"] == "explorative"


async def test_a_future_revision_is_stale_too(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    r = await client.patch(
        f"/course-maps/{created['id']}", json={"title": "x"}, headers=match(99)
    )

    assert r.status_code == 412 and r.json()["detail"]["currentRevision"] == 1


async def test_a_stale_bubble_write_is_412_with_the_current_version(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"
    moved = await client.patch(url, json={"x": 0.5, "y": 0.5}, headers=match(1))
    assert moved.status_code == 200 and moved.json()["version"] == 2

    stale_patch = await client.patch(url, json={"icon": "star"}, headers=match(1))
    stale_delete = await client.delete(url, headers=match(1))

    for r in (stale_patch, stale_delete):
        assert r.status_code == 412
        detail = r.json()["detail"]
        assert detail["code"] == "version_conflict"
        assert detail["currentVersion"] == 2
        assert "currentRevision" not in detail
    still = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"]
    assert len(still) == 1 and still[0]["icon"] is None  # neither write happened


async def test_a_missing_resource_is_404_not_412(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    ghost = "0192f3a0-0000-7000-8000-000000000000"

    missing_map = await client.patch(
        f"/course-maps/{ghost}", json={"title": "x"}, headers=match(1)
    )
    missing_bubble = await client.delete(
        f"/course-maps/{created['id']}/bubbles/{ghost}", headers=match(1)
    )
    missing_appearance = await client.put(
        f"/course-maps/{ghost}/appearance", json=APPEARANCE, headers=match(1)
    )

    assert missing_map.status_code == 404
    assert missing_bubble.status_code == 404
    assert missing_appearance.status_code == 404


# --- success bumps -----------------------------------------------------------------


async def test_successful_map_writes_bump_the_revision_and_return_the_new_etag(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    base = f"/course-maps/{created['id']}"

    patched = await client.patch(base, json={"title": "dos"}, headers=match(1))
    assert (patched.json()["revision"], patched.headers["etag"]) == (2, '"2"')

    appearance = await client.put(
        f"{base}/appearance", json=APPEARANCE, headers=match(2)
    )
    assert (appearance.json()["revision"], appearance.headers["etag"]) == (3, '"3"')
    assert appearance.json()["title"] == "dos"  # the earlier write is kept


async def test_successful_bubble_writes_bump_the_version_and_delete_removes_it(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    moved = await client.patch(url, json={"x": 0.9, "y": 0.9}, headers=match(1))
    assert (moved.json()["version"], moved.headers["etag"]) == (2, '"2"')
    restyled = await client.patch(url, json={"icon": "star"}, headers=match(2))
    assert restyled.json()["version"] == 3

    assert (await client.delete(url, headers=match(3))).status_code == 204
    assert (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"] == []


async def test_a_failed_guarded_write_does_not_bump_anything(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    base = f"/course-maps/{created['id']}"
    await client.patch(base, json={"title": "dos"}, headers=match(7))  # stale

    assert (await client.get(base)).json()["revision"] == 1


# --- the status exemption ------------------------------------------------------------


async def test_a_status_only_patch_needs_no_if_match_and_does_not_bump(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    r = await client.patch(url, json={"status": "complete"})

    assert r.status_code == 200
    assert (r.json()["status"], r.json()["version"]) == ("complete", 1)
    assert r.headers["etag"] == '"1"'


async def test_a_status_only_patch_ignores_a_stale_if_match(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"
    await client.patch(url, json={"x": 0.5, "y": 0.5}, headers=match(1))

    r = await client.patch(url, json={"status": "in_progress"}, headers=match(1))

    assert r.status_code == 200 and r.json()["version"] == 2


async def test_status_mixed_with_content_is_guarded_and_all_or_nothing(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"
    body = {"status": "complete", "icon": "star"}

    no_header = await client.patch(url, json=body)
    stale = await client.patch(url, json=body, headers=match(5))
    assert (no_header.status_code, stale.status_code) == (428, 412)
    unchanged = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"][0]
    assert (unchanged["status"], unchanged["icon"]) == ("locked", None)

    ok = await client.patch(url, json=body, headers=match(1))
    assert ok.status_code == 200
    assert (ok.json()["status"], ok.json()["icon"], ok.json()["version"]) == (
        "complete",
        "star",
        2,
    )


async def test_a_status_write_does_not_conflict_with_an_in_flight_content_edit(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    # Both start from the state the editor loaded (version 1).
    results = await asyncio.gather(
        client.patch(url, json={"x": 0.7, "y": 0.7}, headers=match(1)),
        client.patch(url, json={"status": "complete"}),
    )

    assert [r.status_code for r in results] == [200, 200]
    final = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"][0]
    assert (final["x"], final["status"], final["version"]) == (0.7, "complete", 2)


# --- races ----------------------------------------------------------------------------


async def test_two_writers_on_the_same_bubble_version_exactly_one_wins(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    results = await asyncio.gather(
        *(
            client.patch(url, json={"x": x, "y": x}, headers=match(1))
            for x in (0.3, 0.6, 0.9)
        )
    )

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 412, 412]
    winner = next(r for r in results if r.status_code == 200).json()
    final = (await client.get(f"/course-maps/{created['id']}")).json()["bubbles"][0]
    assert final["version"] == 2 and final["x"] == winner["x"]


async def test_two_writers_on_the_same_map_revision_exactly_one_wins(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    base = f"/course-maps/{created['id']}"

    results = await asyncio.gather(
        client.patch(base, json={"title": "A"}, headers=match(1)),
        client.patch(base, json={"title": "B"}, headers=match(1)),
        client.put(f"{base}/appearance", json=APPEARANCE, headers=match(1)),
    )

    assert sorted(r.status_code for r in results) == [200, 412, 412]
    assert (await client.get(base)).json()["revision"] == 2


async def test_a_concurrent_delete_and_edit_of_one_bubble_cannot_both_win(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bubble = await make_bubble(client, created["id"])
    url = f"/course-maps/{created['id']}/bubbles/{bubble['id']}"

    results = await asyncio.gather(
        client.delete(url, headers=match(1)),
        client.patch(url, json={"x": 0.5, "y": 0.5}, headers=match(1)),
    )

    codes = sorted(r.status_code for r in results)
    assert codes in ([204, 404], [200, 412], [204, 412]), codes
    assert 204 in codes or 200 in codes


# --- what is NOT guarded ----------------------------------------------------------------


async def test_creating_and_ordering_need_no_if_match(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    first = await make_bubble(client, created["id"], 1)
    second = await make_bubble(client, created["id"], 2)

    order = await client.put(
        f"/course-maps/{created['id']}/bubbles/order",
        json={"bubbleIds": [second["id"], first["id"]]},
    )
    maps_order = await client.put(
        "/course-maps/order",
        json={"moodleCourseId": 1, "mapIds": [created["id"]]},
    )

    assert order.status_code == 200 and maps_order.status_code == 200
    # Ordering does not touch the concurrency counters.
    after = (await client.get(f"/course-maps/{created['id']}")).json()
    assert after["revision"] == 1
    assert [b["version"] for b in after["bubbles"]] == [1, 1]
