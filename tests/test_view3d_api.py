"""`PUT /course-maps/{id}/view3d` through the real stack, and what reads show of it."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import app
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


def match(number: int) -> dict[str, str]:
    return {"If-Match": f'"{number}"'}


async def make_map(client: httpx.AsyncClient, course: int = 1) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={"title": "Mapa", "moodleCourseId": course, "imageUrl": "http://i/x.png"},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def make_bubbles(
    client: httpx.AsyncClient, map_id: str, count: int, first_activity: int = 1
) -> list[str]:
    ids = []
    for n in range(count):
        r = await client.post(
            f"/course-maps/{map_id}/bubbles",
            json={"activityId": first_activity + n, "x": 0.1 * (n + 1), "y": 0.5},
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    return ids


def body(*pairs: tuple[int, str], **extra: Any) -> dict[str, Any]:
    return {
        "scene": SCENE,
        "bindings": [{"zoneId": zone, "bubbleId": bubble} for zone, bubble in pairs],
        **extra,
    }


async def put(
    client: httpx.AsyncClient, map_id: str, payload: Any, revision: int | None = None
) -> httpx.Response:
    """`json=None` would send no body at all; a client disables the view by
    sending the JSON text `null`."""
    return await client.put(
        f"/course-maps/{map_id}/view3d",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            **(match(revision) if revision is not None else {}),
        },
    )


# --- reading a map without a 3D view ------------------------------------------


async def test_a_new_map_reads_with_a_null_view3d(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    assert created["view3d"] is None
    fetched = await client.get(f"/course-maps/{created['id']}")
    assert fetched.json()["view3d"] is None


# --- the happy path -----------------------------------------------------------


async def test_put_replaces_the_bindings_and_answers_with_the_map(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    first, second = await make_bubbles(client, created["id"], 2)

    r = await client.put(
        f"/course-maps/{created['id']}/view3d",
        json={
            "scene": SCENE,
            "bindings": [
                {
                    "zoneId": 4,
                    "bubbleId": second,
                    "title": {"es": "  Bienvenida  ", "en": "Welcome"},
                    "description": {"es": "Línea uno\r\nLínea dos"},
                },
                {"zoneId": 0, "bubbleId": first, "title": None, "description": None},
            ],
        },
    )

    assert r.status_code == 200, r.text
    assert r.headers["etag"] == '"2"'
    result = r.json()
    assert result["revision"] == 2
    # Bindings come back ordered by zone; a language that was not given is
    # absent (not null); text is trimmed and its line breaks normalized.
    assert result["view3d"] == {
        "scene": SCENE,
        "bindings": [
            {"zoneId": 0, "bubbleId": first, "title": None, "description": None},
            {
                "zoneId": 4,
                "bubbleId": second,
                "title": {"es": "Bienvenida", "en": "Welcome"},
                "description": {"es": "Línea uno\nLínea dos"},
            },
        ],
    }
    assert (await client.get(f"/course-maps/{created['id']}")).json()["view3d"] == (
        result["view3d"]
    )


async def test_a_second_put_replaces_everything_and_null_removes_the_view(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    first, second = await make_bubbles(client, created["id"], 2)
    await put(client, created["id"], body((0, first), (1, second)))

    swapped = await put(client, created["id"], body((8, second)))
    assert swapped.json()["view3d"]["bindings"] == [
        {"zoneId": 8, "bubbleId": second, "title": None, "description": None}
    ]

    cleared = await put(client, created["id"], None)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["view3d"] is None
    assert cleared.json()["revision"] == 4
    again = await client.get(f"/course-maps/{created['id']}")
    assert again.json()["view3d"] is None


async def test_a_scene_with_no_bindings_is_a_3d_view(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    r = await put(client, created["id"], {"scene": SCENE, "bindings": []})

    assert r.json()["view3d"] == {"scene": SCENE, "bindings": []}


async def test_blank_languages_and_empty_texts_are_not_stored(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    [bubble] = await make_bubbles(client, created["id"], 1)

    r = await put(
        client,
        created["id"],
        {
            "scene": SCENE,
            "bindings": [
                {
                    "zoneId": 1,
                    "bubbleId": bubble,
                    "title": {"es": "   ", "en": "\x00\x07"},
                    "description": {},
                }
            ],
        },
    )

    assert r.status_code == 200, r.text
    [binding] = r.json()["view3d"]["bindings"]
    assert binding["title"] is None and binding["description"] is None


# --- the 422 codes --------------------------------------------------------------


async def test_an_unknown_scene_is_422_scene_unknown(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    r = await put(client, created["id"], {"scene": "atlantis", "bindings": []})

    assert r.status_code == 422
    assert r.json()["detail"] == {
        "code": "scene_unknown",
        "message": "The 3D scene is not one of the available scenes",
        "scene": "atlantis",
        "scenes": [SCENE],
    }


@pytest.mark.parametrize("zone", [9, 10, -1, 10**12])
async def test_a_zone_outside_the_scene_is_422_zone_out_of_range(
    client: httpx.AsyncClient, zone: int
) -> None:
    created = await make_map(client)
    [bubble] = await make_bubbles(client, created["id"], 1)

    r = await put(client, created["id"], body((zone, bubble)))

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "zone_out_of_range"
    assert detail["zoneIds"] == [zone] and detail["zoneCount"] == 9


async def test_the_first_and_last_zone_are_in_range(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    first, last = await make_bubbles(client, created["id"], 2)

    r = await put(client, created["id"], body((0, first), (8, last)))

    assert r.status_code == 200, r.text


async def test_two_bubbles_in_one_zone_is_422_duplicate_zone(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    first, second = await make_bubbles(client, created["id"], 2)

    r = await put(client, created["id"], body((3, first), (3, second)))

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duplicate_zone"
    assert r.json()["detail"]["zoneIds"] == [3]


async def test_one_bubble_in_two_zones_is_422_duplicate_bubble(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    [bubble] = await make_bubbles(client, created["id"], 1)

    r = await put(client, created["id"], body((1, bubble), (2, bubble)))

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duplicate_bubble"
    assert r.json()["detail"]["bubbleIds"] == [bubble]


async def test_a_bubble_of_another_map_or_unknown_is_422_bubble_not_in_map(
    client: httpx.AsyncClient,
) -> None:
    mine = await make_map(client, course=1)
    other = await make_map(client, course=2)
    [own] = await make_bubbles(client, mine["id"], 1)
    [foreign] = await make_bubbles(client, other["id"], 1, first_activity=50)
    ghost = str(uuid.uuid4())

    r = await put(client, mine["id"], body((0, own), (1, foreign), (2, ghost)))

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "bubble_not_in_map"
    assert sorted(detail["bubbleIds"]) == sorted([foreign, ghost])


async def test_malformed_bodies_are_rejected_by_validation(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    [bubble] = await make_bubbles(client, created["id"], 1)

    def with_binding(**fields: Any) -> dict[str, Any]:
        return {
            "scene": SCENE,
            "bindings": [{"zoneId": 0, "bubbleId": bubble, **fields}],
        }

    bad_bodies = [
        {"bindings": []},  # no scene
        {"scene": SCENE},  # no bindings
        {"scene": SCENE, "bindings": [], "extra": 1},
        {"scene": "", "bindings": []},
        {"scene": SCENE, "bindings": [{"zoneId": 0, "bubbleId": "not-a-uuid"}]},
        {"scene": SCENE, "bindings": [{"zoneId": "x", "bubbleId": bubble}]},
        with_binding(title={"es": "x" * 121}),
        with_binding(description={"en": "x" * 601}),
        with_binding(title={"fr": "Bonjour"}),
        with_binding(title={"es": 5}),
        [],
        "scene",
    ]
    for payload in bad_bodies:
        r = await put(client, created["id"], payload)
        assert r.status_code == 422, payload
        assert isinstance(r.json()["detail"], list), payload  # FastAPI's own shape
    assert (await client.get(f"/course-maps/{created['id']}")).json()["view3d"] is None


async def test_text_limits_count_characters_after_cleaning(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    [bubble] = await make_bubbles(client, created["id"], 1)
    # 120 / 600 characters of text, padded with whitespace and control codes
    # that are removed before counting.
    title = "\x01 " + "ñ" * 120 + " \x1f"
    description = "\t" + "d" * 599 + "\n"

    r = await put(
        client,
        created["id"],
        {
            "scene": SCENE,
            "bindings": [
                {
                    "zoneId": 0,
                    "bubbleId": bubble,
                    "title": {"es": title},
                    "description": {"en": description},
                }
            ],
        },
    )

    assert r.status_code == 200, r.text
    [binding] = r.json()["view3d"]["bindings"]
    assert binding["title"] == {"es": "ñ" * 120}
    assert binding["description"] == {"en": "d" * 599}


async def test_text_is_normalized_to_nfc_and_never_interpreted(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    [bubble] = await make_bubbles(client, created["id"], 1)
    decomposed = "Café <b>{{x}}</b> <script>alert(1)</script>"

    r = await put(
        client,
        created["id"],
        {
            "scene": SCENE,
            "bindings": [
                {"zoneId": 0, "bubbleId": bubble, "title": {"es": decomposed}}
            ],
        },
    )

    [binding] = r.json()["view3d"]["bindings"]
    assert binding["title"]["es"] == "Café <b>{{x}}</b> <script>alert(1)</script>"


# --- atomicity ----------------------------------------------------------------


async def test_one_invalid_binding_changes_nothing(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    first, second = await make_bubbles(client, created["id"], 2)
    good = await put(client, created["id"], body((0, first)))
    before = (await client.get(f"/course-maps/{created['id']}")).json()
    assert good.status_code == 200

    r = await put(
        client, created["id"], body((1, second), (2, str(uuid.uuid4())), (3, second))
    )

    assert r.status_code == 422
    after = (await client.get(f"/course-maps/{created['id']}")).json()
    assert after == before  # bindings, scene and revision alike


# --- preconditions --------------------------------------------------------------


async def test_a_put_without_if_match_is_428(plain: httpx.AsyncClient) -> None:
    created = await make_map(plain)

    r = await put(plain, created["id"], {"scene": SCENE, "bindings": []})

    assert r.status_code == 428
    assert r.json()["detail"]["code"] == "precondition_required"
    assert (await plain.get(f"/course-maps/{created['id']}")).json()["view3d"] is None


async def test_a_malformed_if_match_is_400(plain: httpx.AsyncClient) -> None:
    created = await make_map(plain)

    r = await plain.put(
        f"/course-maps/{created['id']}/view3d",
        json={"scene": SCENE, "bindings": []},
        headers={"If-Match": "1"},
    )

    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_if_match"


async def test_a_stale_revision_is_412_and_changes_nothing(
    plain: httpx.AsyncClient,
) -> None:
    created = await make_map(plain)
    [bubble] = await make_bubbles(plain, created["id"], 1)
    first = await put(plain, created["id"], body((0, bubble)), revision=1)
    assert first.status_code == 200 and first.json()["revision"] == 2

    stale = await put(plain, created["id"], None, revision=1)

    assert stale.status_code == 412
    detail = stale.json()["detail"]
    assert detail["code"] == "version_conflict" and detail["currentRevision"] == 2
    assert stale.headers["etag"] == '"2"'
    kept = (await plain.get(f"/course-maps/{created['id']}")).json()["view3d"]
    assert kept is not None and len(kept["bindings"]) == 1


async def test_a_missing_map_is_404(client: httpx.AsyncClient) -> None:
    r = await client.put(
        f"/course-maps/{uuid.uuid4()}/view3d", json={"scene": SCENE, "bindings": []}
    )

    assert r.status_code == 404


async def test_a_null_body_without_if_match_is_still_428(
    plain: httpx.AsyncClient,
) -> None:
    created = await make_map(plain)

    r = await put(plain, created["id"], None)

    assert r.status_code == 428


async def test_an_empty_request_body_does_not_remove_the_view(
    plain: httpx.AsyncClient,
) -> None:
    created = await make_map(plain)
    [bubble] = await make_bubbles(plain, created["id"], 1)
    await put(plain, created["id"], body((0, bubble)), revision=1)

    r = await plain.put(f"/course-maps/{created['id']}/view3d", headers=match(2))

    assert r.status_code == 422  # only an explicit JSON `null` disables the view
    assert (await plain.get(f"/course-maps/{created['id']}")).json()["view3d"] is not (
        None
    )


# --- bubbles and the map ----------------------------------------------------------


async def test_deleting_a_bubble_removes_its_binding(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    keep, doomed = await make_bubbles(client, created["id"], 2)
    await put(client, created["id"], body((0, keep), (1, doomed)))

    deleted = await client.delete(f"/course-maps/{created['id']}/bubbles/{doomed}")
    assert deleted.status_code == 204

    after = (await client.get(f"/course-maps/{created['id']}")).json()
    assert after["view3d"] == {
        "scene": SCENE,
        "bindings": [
            {"zoneId": 0, "bubbleId": keep, "title": None, "description": None}
        ],
    }


async def test_the_zone_of_a_deleted_bubble_can_be_used_again(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    first, second = await make_bubbles(client, created["id"], 2)
    await put(client, created["id"], body((5, first)))
    await client.delete(f"/course-maps/{created['id']}/bubbles/{first}")

    r = await put(client, created["id"], body((5, second)))

    assert r.status_code == 200, r.text


async def test_other_edits_leave_the_3d_view_alone(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    [bubble] = await make_bubbles(client, created["id"], 1)
    await put(client, created["id"], body((2, bubble)))

    renamed = await client.patch(f"/course-maps/{created['id']}", json={"title": "N"})

    assert renamed.json()["view3d"]["bindings"][0]["zoneId"] == 2


async def test_deleting_the_map_takes_the_view_with_it(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    [bubble] = await make_bubbles(client, created["id"], 1)
    await put(client, created["id"], body((2, bubble)))

    deleted = await client.delete(f"/course-maps/{created['id']}")

    assert deleted.status_code == 204
