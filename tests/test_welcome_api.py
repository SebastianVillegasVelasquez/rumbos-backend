"""The welcome curtain through the real stack: PUT appearance, publish, discard,
restore, and who may write it."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.main import app
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.test_appearance_api import DEFAULT_WELCOME
from tests.test_publications_api import full_map, make_map, match, publish, state

CURTAIN: dict[str, Any] = {
    "enabled": True,
    "title": "Bienvenido a {curso}",
    "message": "Explora el mapa.",
    "video": {"provider": "youtube", "videoId": "dQw4w9WgXcQ"},
    "playOnEnter": False,
    "show": "always",
}


@pytest.fixture
async def client(engine: Any) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


async def put_settings(
    client: httpx.AsyncClient, map_id: str, settings: dict[str, Any]
) -> httpx.Response:
    return await client.put(
        f"/course-maps/{map_id}/appearance",
        json={"settings": settings, "defaultSkinId": None, "skinRules": []},
    )


# --- writing and reading ----------------------------------------------------------


async def test_a_curtain_is_saved_and_read_back(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    r = await put_settings(client, created["id"], {"welcome": CURTAIN})

    assert r.status_code == 200, r.text
    assert r.json()["settings"]["welcome"] == CURTAIN
    assert (await full_map(client, created["id"]))["settings"]["welcome"] == CURTAIN


async def test_a_map_without_a_curtain_reads_the_defaults(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    assert created["settings"]["welcome"] == DEFAULT_WELCOME


@pytest.mark.parametrize("clear", [{"welcome": None}, {}, {"welcome": DEFAULT_WELCOME}])
async def test_clearing_the_curtain_returns_to_the_unconfigured_map(
    client: httpx.AsyncClient, clear: dict[str, Any]
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    baseline = (await state(client, map_id))["draftHash"]
    await put_settings(client, map_id, {"welcome": CURTAIN})
    assert (await state(client, map_id))["draftHash"] != baseline

    r = await put_settings(client, map_id, clear)

    assert r.status_code == 200, r.text
    assert r.json()["settings"]["welcome"] == DEFAULT_WELCOME
    assert (await state(client, map_id))["draftHash"] == baseline


async def test_disabling_the_curtain_is_stored(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    r = await put_settings(client, created["id"], {"welcome": {"enabled": False}})

    assert r.json()["settings"]["welcome"] == {**DEFAULT_WELCOME, "enabled": False}


async def test_saving_back_what_was_read_changes_nothing(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    before = (await state(client, map_id))["draftHash"]

    r = await put_settings(client, map_id, created["settings"])

    assert r.status_code == 200, r.text
    assert (await state(client, map_id))["draftHash"] == before


# --- invalid ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("welcome", "path", "code"),
    [
        ({"title": "<b>{nombre}</b>"}, "title", "welcome_invalid_placeholder"),
        ({"message": "Hola {alumno}"}, "message", "welcome_invalid_placeholder"),
        ({"title": ""}, "title", "too_short"),
        ({"title": "a" * 121}, "title", "too_long"),
        ({"message": "a" * 301}, "message", "too_long"),
        (
            {"video": {"provider": "youtube", "videoId": "x/../y?z=1"}},
            "video.videoId",
            "invalid_video_id",
        ),
        (
            {"video": {"provider": "youtube", "videoId": "https://evil.test/a"}},
            "video.videoId",
            "invalid_video_id",
        ),
        (
            {"video": {"provider": "dailymotion", "videoId": "abcdef"}},
            "video.provider",
            "invalid_value",
        ),
        (
            {"video": {"url": "https://youtu.be/dQw4w9WgXcQ"}},
            "video.url",
            "unknown_field",
        ),
        ({"video": "https://youtu.be/dQw4w9WgXcQ"}, "video", "invalid_type"),
        ({"show": "sometimes"}, "show", "invalid_value"),
        ({"enabled": "yes"}, "enabled", "invalid_type"),
        ({"colour": "red"}, "colour", "unknown_field"),
    ],
)
async def test_an_invalid_curtain_is_422_welcome_invalid(
    client: httpx.AsyncClient, welcome: dict[str, Any], path: str, code: str
) -> None:
    created = await make_map(client)

    r = await put_settings(client, created["id"], {"welcome": welcome})

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "welcome_invalid"
    assert {"path": path, "code": code} in [
        {"path": p["path"], "code": p["code"]} for p in detail["problems"]
    ]
    assert all(p["message"] for p in detail["problems"])


async def test_every_problem_is_listed_and_the_input_is_not_echoed(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    hostile = "{hostile-secret}"

    r = await put_settings(
        client,
        created["id"],
        {"welcome": {"title": hostile, "message": "a" * 301, "show": "no"}},
    )

    problems = r.json()["detail"]["problems"]
    assert sorted(p["path"] for p in problems) == ["message", "show", "title"]
    assert hostile not in r.text


async def test_an_error_outside_the_curtain_keeps_the_generic_answer(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    r = await put_settings(client, created["id"], {"mode": "sideways", "welcome": {}})

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"][-1] == "mode"


async def test_a_bad_welcome_next_to_a_bad_setting_gets_the_generic_answer(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    r = await put_settings(
        client, created["id"], {"mode": "sideways", "welcome": {"show": "no"}}
    )

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


async def test_an_invalid_curtain_leaves_the_whole_appearance_untouched(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await put_settings(client, map_id, {"mode": "guided", "welcome": CURTAIN})
    before = await full_map(client, map_id)

    r = await client.put(
        f"/course-maps/{map_id}/appearance",
        json={
            "settings": {"mode": "explorative", "welcome": {"title": "{mal}"}},
            "defaultSkinId": None,
            "skinRules": [
                {"modname": "quiz", "skinId": "0" * 8 + "-0000-4000-8000-" + "0" * 12}
            ],
        },
    )

    assert r.status_code == 422
    after = await full_map(client, map_id)
    assert after["settings"] == before["settings"]
    assert after["skinRules"] == before["skinRules"] == []
    assert after["revision"] == before["revision"]


# --- publishing -------------------------------------------------------------------


async def test_a_map_published_without_a_curtain_stays_up_to_date(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await publish(client, created["id"])

    # Saving the defaults back is not a change either.
    await put_settings(client, created["id"], {"welcome": None})

    current = await state(client, created["id"])
    assert current["status"] == "up_to_date"


async def test_the_curtain_is_published_and_counts_as_a_settings_change(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await publish(client, map_id)

    await put_settings(client, map_id, {"welcome": CURTAIN})

    current = await state(client, map_id)
    assert current["status"] == "unpublished_changes"
    assert current["changes"]["settingsChanged"] is True
    await publish(client, map_id)
    shown = (await client.get(f"/published/course-maps/{map_id}")).json()
    assert shown["settings"]["welcome"] == CURTAIN
    assert (await state(client, map_id))["status"] == "up_to_date"


async def test_discard_brings_the_published_curtain_back(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await put_settings(client, map_id, {"welcome": CURTAIN})
    await publish(client, map_id)
    await put_settings(client, map_id, {"welcome": {"enabled": False}})
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/discard-changes", headers=match(revision)
    )

    assert r.status_code == 200, r.text
    assert r.json()["map"]["settings"]["welcome"] == CURTAIN
    assert (await state(client, map_id))["status"] == "up_to_date"


async def test_restoring_a_publication_without_a_curtain_clears_it(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await publish(client, map_id, "sin cortina")
    await put_settings(client, map_id, {"welcome": CURTAIN})
    await publish(client, map_id, "con cortina")
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/publications/1/restore", headers=match(revision)
    )

    assert r.status_code == 200, r.text
    assert r.json()["map"]["settings"]["welcome"] == DEFAULT_WELCOME
    # The draft equals publication 1 again, not the current publication 2.
    assert (await state(client, map_id))["status"] == "unpublished_changes"


async def test_restoring_a_publication_with_a_curtain_brings_it_back(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await put_settings(client, map_id, {"welcome": CURTAIN})
    await publish(client, map_id, "con cortina")
    await put_settings(client, map_id, {"welcome": None})
    await publish(client, map_id, "sin cortina")
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/publications/1/restore", headers=match(revision)
    )

    assert r.json()["map"]["settings"]["welcome"] == CURTAIN
