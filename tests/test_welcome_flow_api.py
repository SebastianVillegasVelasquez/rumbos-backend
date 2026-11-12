"""The welcome flow through the real stack: PUT appearance, publish, discard,
restore. Documents (pdf steps) have their own tests."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.main import app
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.test_appearance_api import DEFAULT_WELCOME
from tests.test_publications_api import full_map, make_map, match, publish, state
from tests.test_welcome_api import put_settings


@pytest.fixture
async def client(engine: Any) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


FLOW: list[dict[str, Any]] = [
    {
        "type": "welcome",
        "key": "bienvenida",
        "title": "Bienvenida",
        "message": "Hola {curso}",
        "buttonLabel": "Empezar",
    },
    {
        "type": "video",
        "key": "videointro",
        "title": "Video",
        "video": {"provider": "vimeo", "videoId": "76979871"},
        "allowFallback": False,
    },
    {
        "type": "html",
        "key": "lectura01",
        "title": "Lectura",
        "html": "<h2>Instrucciones</h2><p>Lee con atención.</p>",
        "css": "h2 { color: #036 }",
        "ack": {"label": "Lo he leído"},
        "minSeconds": 20,
    },
    {
        "type": "quiz",
        "key": "cuestionario",
        "title": "Cuestionario de entrada",
        "cmid": 77,
        "intro": "Responde.",
        "outcomeAbove": "Bien",
        "outcomeBelow": "Repasa",
    },
]


def curtain(**fields: Any) -> dict[str, Any]:
    return {"steps": FLOW, **fields}


def read_back(step: dict[str, Any]) -> dict[str, Any]:
    """A step as the API returns it: every optional field present."""
    defaults: dict[str, Any] = {"mandatory": True, "revision": 1}
    return {**defaults, **step}


# --- writing and reading --------------------------------------------------------------


async def test_a_flow_is_saved_and_read_back(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    r = await put_settings(client, created["id"], {"welcome": curtain(show="always")})

    assert r.status_code == 200, r.text
    welcome = r.json()["settings"]["welcome"]
    assert [s["key"] for s in welcome["steps"]] == [s["key"] for s in FLOW]
    assert welcome["show"] == "always"
    # A flow replaces the legacy fields: they read as null.
    assert welcome["title"] is None and welcome["message"] is None
    assert welcome["video"] is None and welcome["playOnEnter"] is None
    again = (await full_map(client, created["id"]))["settings"]["welcome"]
    assert again == welcome


async def test_every_default_is_filled_in(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    minimal = [
        {"type": "html", "key": "lectura01", "title": "L", "html": "<p>x</p>"},
        {"type": "quiz", "key": "cuestionario", "title": "Q"},
    ]

    r = await put_settings(client, created["id"], {"welcome": {"steps": minimal}})

    html, quiz = r.json()["settings"]["welcome"]["steps"]
    assert html["mandatory"] is True and html["revision"] == 1
    assert html["minSeconds"] == 0 and html["ack"] is None and html["css"] is None
    assert quiz["cmid"] is None and quiz["intro"] is None


async def test_removing_the_steps_goes_back_to_the_legacy_curtain(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await put_settings(client, created["id"], {"welcome": curtain()})

    r = await put_settings(client, created["id"], {"welcome": {"title": "Hola"}})

    welcome = r.json()["settings"]["welcome"]
    assert welcome["steps"] is None and welcome["title"] == "Hola"
    assert welcome["playOnEnter"] is True


async def test_a_legacy_curtain_still_works_exactly_as_before(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    assert created["settings"]["welcome"] == DEFAULT_WELCOME

    legacy = {
        "enabled": True,
        "title": "Bienvenido",
        "message": "Explora.",
        "video": {"provider": "youtube", "videoId": "dQw4w9WgXcQ"},
        "playOnEnter": False,
        "show": "always",
    }
    r = await put_settings(client, created["id"], {"welcome": legacy})

    assert r.status_code == 200, r.text
    assert r.json()["settings"]["welcome"] == {**legacy, "steps": None}


# --- refused --------------------------------------------------------------------------


async def test_structural_problems_are_422_with_paths(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    steps = [
        {"type": "html", "key": "BAD", "title": "", "html": "<p>x</p>"},
        {"type": "poll", "key": "otraclave", "title": "t"},
    ]

    r = await put_settings(client, created["id"], {"welcome": {"steps": steps}})

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "welcome_invalid"
    found = {(p["path"], p["code"]) for p in detail["problems"]}
    assert ("welcome.steps[0].key", "invalid_format") in found
    assert ("welcome.steps[0].title", "too_short") in found
    assert ("welcome.steps[1]", "step_type_invalid") in found
    assert all(p["message"] for p in detail["problems"])


async def test_flow_rules_are_422_with_every_problem_listed(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    steps = [
        {
            "type": "html",
            "key": "lectura01",
            "title": "A",
            "html": "<script>x</script>",
        },
        {
            "type": "html",
            "key": "lectura01",
            "title": "B",
            "html": "<p>ok</p>",
            "css": "@import 'x';",
        },
        {"type": "welcome", "key": "bienvenida", "title": "W"},
    ]

    r = await put_settings(client, created["id"], {"welcome": {"steps": steps}})

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "welcome_invalid"
    found = {(p["path"], p["code"]) for p in detail["problems"]}
    assert found == {
        ("welcome.steps[1].key", "step_key_duplicate"),
        ("welcome.steps[2]", "welcome_step_position"),
        ("welcome.steps[0].html", "html_forbidden_markup"),
        ("welcome.steps[1].css", "css_forbidden"),
    }
    markup = next(p for p in detail["problems"] if p["code"] == "html_forbidden_markup")
    assert "<script>" in markup["message"]
    assert "x</script>" not in r.text


async def test_legacy_fields_next_to_steps_are_422(client: httpx.AsyncClient) -> None:
    created = await make_map(client)

    r = await put_settings(
        client,
        created["id"],
        {"welcome": curtain(title="Hola", playOnEnter=True)},
    )

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "welcome_invalid"
    assert {(p["path"], p["code"]) for p in detail["problems"]} == {
        ("welcome.title", "legacy_fields_with_steps"),
        ("welcome.playOnEnter", "legacy_fields_with_steps"),
    }


async def test_a_refused_flow_leaves_the_whole_appearance_untouched(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await put_settings(client, map_id, {"mode": "guided", "welcome": curtain()})
    before = await full_map(client, map_id)
    hostile = {"type": "html", "key": "lectura99", "title": "t", "html": "<iframe>"}

    r = await put_settings(
        client,
        map_id,
        {"mode": "explorative", "welcome": {"steps": [hostile]}},
    )

    assert r.status_code == 422
    after = await full_map(client, map_id)
    assert after["settings"] == before["settings"]
    assert after["revision"] == before["revision"]


async def test_a_flow_next_to_another_bad_setting_gets_the_generic_answer(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)

    r = await put_settings(
        client, created["id"], {"mode": "sideways", "welcome": curtain()}
    )

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


# --- publishing -----------------------------------------------------------------------


async def test_a_flow_is_published_and_counts_as_a_settings_change(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await publish(client, map_id)

    await put_settings(client, map_id, {"welcome": curtain()})

    current = await state(client, map_id)
    assert current["status"] == "unpublished_changes"
    assert current["changes"]["settingsChanged"] is True
    await publish(client, map_id)
    shown = (await client.get(f"/published/course-maps/{map_id}")).json()
    assert [s["key"] for s in shown["settings"]["welcome"]["steps"]] == [
        s["key"] for s in FLOW
    ]
    assert (await state(client, map_id))["status"] == "up_to_date"


async def test_saving_the_same_flow_again_is_not_a_change(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    saved = await put_settings(client, map_id, {"welcome": curtain()})
    await publish(client, map_id)

    # What the editor reads back is what it saves back.
    await put_settings(client, map_id, {"welcome": saved.json()["settings"]["welcome"]})

    assert (await state(client, map_id))["status"] == "up_to_date"


async def test_a_new_revision_of_a_step_is_a_change(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await put_settings(client, map_id, {"welcome": curtain()})
    await publish(client, map_id)
    bumped = [{**s, "revision": 2} if s["key"] == "lectura01" else s for s in FLOW]

    await put_settings(client, map_id, {"welcome": {"steps": bumped}})

    assert (await state(client, map_id))["status"] == "unpublished_changes"


async def test_discard_brings_the_published_flow_back(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    saved = await put_settings(client, map_id, {"welcome": curtain()})
    await publish(client, map_id)
    await put_settings(client, map_id, {"welcome": {"title": "Otra cosa"}})
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/discard-changes", headers=match(revision)
    )

    assert r.status_code == 200, r.text
    assert r.json()["map"]["settings"]["welcome"] == saved.json()["settings"]["welcome"]
    assert (await state(client, map_id))["status"] == "up_to_date"


async def test_restoring_a_publication_with_a_flow_brings_it_back(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    saved = await put_settings(client, map_id, {"welcome": curtain()})
    await publish(client, map_id, "con flujo")
    await put_settings(client, map_id, {"welcome": None})
    await publish(client, map_id, "sin flujo")
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/publications/1/restore", headers=match(revision)
    )

    assert r.status_code == 200, r.text
    assert r.json()["map"]["settings"]["welcome"] == saved.json()["settings"]["welcome"]


async def test_restoring_a_publication_without_a_flow_clears_it(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    map_id = created["id"]
    await publish(client, map_id, "sin flujo")
    await put_settings(client, map_id, {"welcome": curtain()})
    await publish(client, map_id, "con flujo")
    revision = (await full_map(client, map_id))["revision"]

    r = await client.post(
        f"/course-maps/{map_id}/publications/1/restore", headers=match(revision)
    )

    assert r.json()["map"]["settings"]["welcome"] == DEFAULT_WELCOME
