"""The congratulations texts: plain text only, a short whitelist of placeholders,
length caps, and refusals reported as `errors[{path, rule, message_key}]`."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.main import app
from app.schemas.congratulations import (
    BODY_MAX,
    CTA_LABEL_MAX,
    TITLE_MAX,
    Congratulations,
    CongratulationsText,
)
from app.schemas.map_settings import MapSettings
from app.trainer.settings_check import congratulations_problems
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.test_publications_api import full_map, make_map, publish
from tests.test_welcome_api import put_settings


@pytest.fixture
async def client(engine: Any) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


def texts(**fields: Any) -> Congratulations:
    return Congratulations.model_validate({"checkin": fields})


def rules(found: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(p["path"], p["rule"]) for p in found]


# --- the shape and the stored form -----------------------------------------------------


def test_no_texts_is_the_default_and_is_not_stored() -> None:
    assert MapSettings().congratulations is None
    assert "congratulations" not in MapSettings().to_stored()
    assert (
        "congratulations"
        not in MapSettings.model_validate({"congratulations": None}).to_stored()
    )


@pytest.mark.parametrize(
    "empty",
    [{}, {"checkin": None, "final": None}, {"checkin": {}}, {"final": {"title": "  "}}],
)
def test_texts_that_say_nothing_are_not_stored(empty: dict[str, Any]) -> None:
    settings = MapSettings.model_validate({"congratulations": empty})
    assert "congratulations" not in settings.to_stored()


def test_a_map_stored_before_the_texts_keeps_its_stored_form() -> None:
    before = MapSettings.model_validate({"mode": "guided"}).to_stored()
    assert MapSettings.model_validate(before).to_stored() == before


def test_the_texts_are_stored_camel_cased_and_round_trip() -> None:
    settings = MapSettings.model_validate(
        {
            "congratulations": {
                "checkin": {
                    "title": "Bien!",
                    "body": "Sacaste {score}",
                    "ctaLabel": "Ir",
                }
            }
        }
    )
    stored = settings.to_stored()
    assert stored["congratulations"] == {
        "checkin": {"title": "Bien!", "body": "Sacaste {score}", "ctaLabel": "Ir"},
        "final": None,
    }
    assert MapSettings.model_validate(stored).to_stored() == stored


def test_a_text_is_cleaned_like_the_others_and_empty_is_null() -> None:
    t = CongratulationsText.model_validate(
        {
            "title": "  Muy\tbien\nahora \u200b\x00 ",
            "body": "Uno\r\nDos\x07",
            "ctaLabel": " ",
        }
    )
    assert t.title == "Muy bien ahora"
    assert t.body == "Uno\nDos"
    assert t.cta_label is None


@pytest.mark.parametrize(
    "extra", [{"html": "<b>x</b>"}, {"url": "https://x.test"}, {"cta_url": "x"}]
)
def test_no_undeclared_field_is_accepted(extra: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CongratulationsText.model_validate({"title": "x", **extra})
    with pytest.raises(ValueError):
        Congratulations.model_validate({"other": {"title": "x"}})


@pytest.mark.parametrize("value", [1, True, ["x"], {"a": 1}])
def test_a_text_must_be_a_string(value: Any) -> None:
    with pytest.raises(ValueError):
        CongratulationsText.model_validate({"title": value})


# --- length ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "limit"),
    [("title", TITLE_MAX), ("body", BODY_MAX), ("ctaLabel", CTA_LABEL_MAX)],
)
def test_each_field_has_its_cap(field: str, limit: int) -> None:
    assert congratulations_problems(texts(**{field: "a" * limit})) == []
    found = congratulations_problems(texts(**{field: "a" * (limit + 1)}))
    assert rules(found) == [
        (f"congratulations.checkin.{field}", "congratulations_too_long")
    ]


def test_the_cap_counts_characters_after_cleaning() -> None:
    padded = "  " + "a" * TITLE_MAX + "\u200b\u200b  "
    assert congratulations_problems(texts(title=padded)) == []


def test_the_final_text_is_checked_too() -> None:
    both = Congratulations.model_validate(
        {"checkin": {"title": "ok"}, "final": {"title": "a" * (TITLE_MAX + 1)}}
    )
    assert rules(congratulations_problems(both)) == [
        ("congratulations.final.title", "congratulations_too_long")
    ]


# --- plain text -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "<b>Bien</b>",
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "a > b",
        "1 < 2",
        "<!-- x -->",
        '<a href="javascript:alert(1)">x</a>',
    ],
)
def test_markup_is_refused_in_every_field(text: str) -> None:
    for field in ("title", "body", "ctaLabel"):
        found = congratulations_problems(texts(**{field: text}))
        assert rules(found) == [
            (f"congratulations.checkin.{field}", "congratulations_markup_not_allowed")
        ]


@pytest.mark.parametrize(
    "text",
    ["&lt;b&gt;", "Tom & Jerry", "100% seguro", "a/b", "¡Felicidades! 🎉", "x\ny"],
)
def test_other_plain_characters_are_fine(text: str) -> None:
    assert congratulations_problems(texts(body=text)) == []


# --- placeholders -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "{score}",
        "{threshold}",
        "Sacaste {score} de {threshold}",
        "{score}{score}{threshold}",
        "${score}",  # a dollar sign is just a character
        "%(score)s",  # nothing here ever formats with %
        "sin placeholders",
    ],
)
def test_the_two_placeholders_are_allowed(text: str) -> None:
    assert congratulations_problems(texts(body=text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        "{0}",
        "{curso}",
        "{name}",
        "{Score}",
        "{SCORE}",
        "{ score }",
        "{score }",
        "{score!r}",
        "{score:10}",
        "{score.__class__}",
        "{score.__class__.__mro__}",
        "{threshold[0]}",
        "{__import__('os').system('id')}",
        "{{score}}",
        "{{7*7}}",
        "{score",
        "score}",
        "{score}}",
        "{{score}",
        "{",
        "}",
        "a } b",
    ],
)
def test_anything_else_in_braces_is_refused(text: str) -> None:
    found = congratulations_problems(texts(body=text))
    assert rules(found) == [
        ("congratulations.checkin.body", "congratulations_placeholder_unknown")
    ]


def test_a_hidden_character_cannot_split_a_placeholder_to_slip_past() -> None:
    # Cleaning removes the zero-width space, so what is checked (and stored) is
    # exactly `{score}`, a valid placeholder, and never `{scor` + `e}`.
    t = CongratulationsText.model_validate({"body": "{scor\u200be}"})
    assert t.body == "{score}"
    assert congratulations_problems(Congratulations(checkin=t)) == []


def test_every_problem_of_a_text_is_reported() -> None:
    found = congratulations_problems(texts(title="<b>{x}</b>" + "a" * TITLE_MAX))
    assert {r for _, r in rules(found)} == {
        "congratulations_too_long",
        "congratulations_markup_not_allowed",
        "congratulations_placeholder_unknown",
    }


def test_the_findings_never_echo_the_text() -> None:
    secret = "<script>TOPSECRET</script>{evil}"
    found = congratulations_problems(texts(body=secret))
    assert "TOPSECRET" not in json.dumps(found)


# --- through the API ----------------------------------------------------------------------------


GOOD = {
    "checkin": {
        "title": "¡Lo lograste!",
        "body": "Tu puntaje fue {score} y el mínimo era {threshold}.",
        "ctaLabel": "Continuar",
    },
    "final": {"title": "¡Curso completado!"},
}


async def test_the_texts_are_saved_read_back_and_published(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(client, created["id"], {"congratulations": GOOD})
    assert r.status_code == 200, r.text
    saved = (await full_map(client, created["id"]))["settings"]["congratulations"]
    assert saved["checkin"] == GOOD["checkin"]
    assert saved["final"] == {
        "title": "¡Curso completado!",
        "body": None,
        "ctaLabel": None,
    }
    await publish(client, created["id"])


async def test_a_map_without_texts_reads_null(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    r = await put_settings(client, created["id"], {"mode": "guided"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["congratulations"] is None
    again = await put_settings(client, created["id"], r.json()["settings"])
    assert again.status_code == 200, again.text
    assert again.json()["settings"] == r.json()["settings"]


async def test_clearing_the_texts_gives_back_the_defaults(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await put_settings(client, created["id"], {"congratulations": GOOD})
    r = await put_settings(client, created["id"], {"congratulations": None})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["congratulations"] is None


async def test_a_bad_text_is_refused_with_the_errors_shape(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    bad = {
        "checkin": {"title": "<b>hola</b>", "ctaLabel": "x" * (CTA_LABEL_MAX + 1)},
        "final": {"body": "Tienes {nombre}"},
    }
    r = await put_settings(client, created["id"], {"congratulations": bad})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "trainer_settings_invalid"
    assert detail["errors"] == [
        {
            "path": "congratulations.checkin.title",
            "rule": "congratulations_markup_not_allowed",
            "message_key": "trainer_settings.congratulations_markup_not_allowed",
        },
        {
            "path": "congratulations.checkin.cta_label",
            "rule": "congratulations_too_long",
            "message_key": "trainer_settings.congratulations_too_long",
        },
        {
            "path": "congratulations.final.body",
            "rule": "congratulations_placeholder_unknown",
            "message_key": "trainer_settings.congratulations_placeholder_unknown",
        },
    ]
    # Nothing the user typed comes back, and nothing was saved.
    assert "hola" not in r.text and "nombre" not in r.text
    after = (await full_map(client, created["id"]))["settings"]
    assert after["congratulations"] is None


async def test_an_unknown_field_is_a_validation_error_not_a_save(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(
        client, created["id"], {"congratulations": {"checkin": {"html": "<b>x</b>"}}}
    )
    assert r.status_code == 422
    after = (await full_map(client, created["id"]))["settings"]
    assert after["congratulations"] is None
