"""The failure, exhausted and route texts: the rules of the congratulations texts,
each block with its own placeholders."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.main import app
from app.schemas.congratulations import BODY_MAX, CTA_LABEL_MAX, TITLE_MAX
from app.schemas.map_settings import MapSettings
from app.trainer.settings_check import journey_text_problems
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.test_publications_api import full_map, make_map, publish
from tests.test_welcome_api import put_settings

BLOCKS = ("failure", "exhausted", "routeInstructions")
FAILURE_PLACEHOLDERS = ("{attempt}", "{attempts_max}", "{attempts_left}", "{threshold}")


@pytest.fixture
async def client(engine: Any) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


def settings(block: str, **fields: Any) -> MapSettings:
    return MapSettings.model_validate({block: fields})


def rules(block: str, **fields: Any) -> list[tuple[str, str]]:
    found = journey_text_problems(settings(block, **fields))
    return [(p["path"], p["rule"]) for p in found]


# --- the shape and the stored form -----------------------------------------------------


def test_unset_blocks_are_null_and_not_stored() -> None:
    read = MapSettings.model_validate({}).model_dump(by_alias=True)
    assert [read[b] for b in BLOCKS] == [None, None, None]
    assert not set(BLOCKS) & set(MapSettings().to_stored())


@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize("empty", [None, {}, {"title": "  ", "body": "\u200b"}])
def test_a_block_that_says_nothing_is_not_stored(block: str, empty: Any) -> None:
    assert block not in MapSettings.model_validate({block: empty}).to_stored()


def test_a_map_stored_before_the_blocks_keeps_its_stored_form() -> None:
    before = MapSettings.model_validate(
        {"mode": "guided", "congratulations": {"final": {"title": "Bien"}}}
    ).to_stored()
    assert MapSettings.model_validate(before).to_stored() == before
    assert not set(BLOCKS) & set(before)


@pytest.mark.parametrize("block", BLOCKS)
def test_a_block_is_stored_camel_cased_cleaned_and_round_trips(block: str) -> None:
    stored = settings(
        block, title="  Hola\u200b ", body="uno\ndos", ctaLabel="Seguir"
    ).to_stored()
    assert stored[block] == {"title": "Hola", "body": "uno\ndos", "ctaLabel": "Seguir"}
    assert MapSettings.model_validate(stored).to_stored() == stored
    assert not (set(BLOCKS) - {block}) & set(stored)


@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize(
    "fields", [{"html": "<b>x</b>"}, {"title": 3}, {"body": ["x"]}, {"cta": "x"}]
)
def test_only_the_three_text_fields_are_accepted(
    block: str, fields: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        settings(block, **fields)


# --- length and markup ------------------------------------------------------------------


@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize(
    ("field", "limit"),
    [("title", TITLE_MAX), ("body", BODY_MAX), ("ctaLabel", CTA_LABEL_MAX)],
)
def test_each_field_has_the_congratulations_cap(
    block: str, field: str, limit: int
) -> None:
    assert rules(block, **{field: "a" * limit}) == []
    assert rules(block, **{field: "a" * (limit + 1)}) == [
        (f"{block}.{field}", "journey_text_too_long")
    ]


@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        '<a href="javascript:alert(1)">x</a>',
        "a > b",
        "1 < 2",
    ],
)
def test_markup_is_refused_in_every_field(block: str, text: str) -> None:
    for field in ("title", "body", "ctaLabel"):
        assert rules(block, **{field: text}) == [
            (f"{block}.{field}", "journey_text_markup_not_allowed")
        ]


# --- placeholders -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        *FAILURE_PLACEHOLDERS,
        "Intento {attempt} de {attempts_max}, quedan {attempts_left}: {threshold}%",
        "{attempt}{attempt}",
        "sin placeholders",
    ],
)
def test_the_failure_text_takes_its_four_placeholders(text: str) -> None:
    assert rules("failure", body=text) == []


@pytest.mark.parametrize(
    "text",
    [
        "{score}",
        "{attempts}",
        "{Attempt}",
        "{ attempt }",
        "{attempt!r}",
        "{attempt:10}",
        "{threshold.__class__}",
        "{__import__('os').system('id')}",
        "{{attempt}}",
        "{{7*7}}",
        "${jndi:ldap://x}",
        "{attempt",
        "attempt}",
        "{}",
        # Spliced around an allowed one: removing the inner one must not help.
        "{atte{attempt}mpt}",
        "{attempts_{attempt}max}",
        "{thres{attempts_left}hold}",
    ],
)
def test_the_failure_text_refuses_any_other_brace(text: str) -> None:
    assert rules("failure", body=text) == [
        ("failure.body", "journey_text_placeholder_unknown")
    ]


@pytest.mark.parametrize("block", ["exhausted", "routeInstructions"])
@pytest.mark.parametrize(
    "text", [*FAILURE_PLACEHOLDERS, "{score}", "{curso}", "{", "}"]
)
def test_the_other_blocks_take_no_placeholder(block: str, text: str) -> None:
    for field in ("title", "body", "ctaLabel"):
        assert rules(block, **{field: text}) == [
            (f"{block}.{field}", "journey_text_placeholder_unknown")
        ]


def test_a_hidden_character_cannot_split_a_placeholder_to_slip_past() -> None:
    cleaned = settings("failure", body="{atte\u200bmpt}")
    assert cleaned.failure and cleaned.failure.body == "{attempt}"
    assert journey_text_problems(cleaned) == []
    assert rules("exhausted", body="{atte\u200bmpt}") == [
        ("exhausted.body", "journey_text_placeholder_unknown")
    ]


# --- through the API ------------------------------------------------------------------------

GOOD = {
    "failure": {
        "title": "Casi",
        "body": "Intento {attempt} de {attempts_max}; quedan {attempts_left}.",
        "ctaLabel": "Volver",
    },
    "exhausted": {"title": "Sin intentos", "body": "Revisa el mapa.", "ctaLabel": None},
    "routeInstructions": {"title": None, "body": "Recorre el mapa.", "ctaLabel": None},
}


async def test_the_blocks_are_saved_read_back_published_and_cleared(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(client, created["id"], GOOD)
    assert r.status_code == 200, r.text
    saved = (await full_map(client, created["id"]))["settings"]
    assert {b: saved[b] for b in BLOCKS} == GOOD
    # What students read is the publication.
    await publish(client, created["id"])
    published = (await client.get(f"/published/course-maps/{created['id']}")).json()
    assert {b: published["settings"][b] for b in BLOCKS} == GOOD

    cleared = await put_settings(client, created["id"], dict.fromkeys(BLOCKS))
    assert cleared.status_code == 200, cleared.text
    assert [cleared.json()["settings"][b] for b in BLOCKS] == [None, None, None]


async def test_a_bad_block_is_refused_with_the_errors_shape_and_nothing_saved(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bad = {
        "failure": {"body": "Tienes {nombre}"},
        "exhausted": {"title": "<b>hola</b>"},
        "routeInstructions": {"ctaLabel": "x" * (CTA_LABEL_MAX + 1)},
    }
    r = await put_settings(client, created["id"], bad)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "trainer_settings_invalid"
    assert detail["errors"] == [
        {
            "path": path,
            "rule": rule,
            "message_key": f"trainer_settings.{rule}",
        }
        for path, rule in [
            ("failure.body", "journey_text_placeholder_unknown"),
            ("exhausted.title", "journey_text_markup_not_allowed"),
            ("route_instructions.cta_label", "journey_text_too_long"),
        ]
    ]
    assert "hola" not in r.text and "nombre" not in r.text
    after = (await full_map(client, created["id"]))["settings"]
    assert [after[b] for b in BLOCKS] == [None, None, None]
