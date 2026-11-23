"""MapSettings.trainer / .assessments / the assessment welcome step."""

import dataclasses
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.main import app
from app.schemas.map_settings import MapSettings
from app.trainer.catalog import Catalog, load_catalog
from app.trainer.dependencies import get_catalog
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.test_publications_api import full_map, make_map
from tests.test_welcome_api import put_settings

EXAMS: dict[str, Any] = {
    "enabled": True,
    "checkIn": {"formId": "a1-form-a", "maxAttempts": 1, "timeLimitMin": 60},
    "final": {"formId": "a1-form-a", "maxAttempts": 3, "timeLimitMin": 60},
    "passPercent": 80,
    "passOperator": "gt",
    "exemption": {"enabled": True},
}


@pytest.fixture
async def client(engine: Any) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


def test_defaults_are_off_and_left_out_of_the_stored_form() -> None:
    settings = MapSettings()
    assert settings.trainer.enabled is False
    assert settings.assessments.enabled is False
    assert settings.assessments.pass_percent == 80
    assert settings.assessments.pass_operator == "gt"
    stored = settings.to_stored()
    assert "trainer" not in stored and "assessments" not in stored


def test_a_configured_exam_is_stored() -> None:
    settings = MapSettings.model_validate({"assessments": EXAMS})
    assert settings.to_stored()["assessments"]["checkIn"]["formId"] == "a1-form-a"


@pytest.mark.parametrize(
    "bad",
    [
        {"passOperator": "lt"},
        {"passPercent": 0},
        {"passPercent": 101},
        {"passPercent": 80.5},
        {"checkIn": {"formId": "x", "maxAttempts": 0}},
        {"checkIn": {"formId": "x", "timeLimitMin": -5}},
        {"checkIn": {"formId": ""}},
        {"checkIn": {"formId": "x", "surprise": 1}},
    ],
)
def test_assessment_settings_are_validated(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MapSettings.model_validate({"assessments": {"enabled": True, **bad}})


def test_trainer_level_is_a_closed_set() -> None:
    with pytest.raises(ValidationError):
        MapSettings.model_validate({"trainer": {"level": "C2"}})


async def test_valid_settings_round_trip(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    body = {"assessments": EXAMS, "trainer": {"enabled": True, "level": "A1"}}
    r = await put_settings(client, created["id"], body)
    assert r.status_code == 200, r.text
    stored = (await full_map(client, created["id"]))["settings"]
    assert stored["assessments"] == EXAMS
    assert stored["trainer"]["enabled"] is True


async def test_an_unknown_form_is_a_422(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    exams = {**EXAMS, "checkIn": {"formId": "nope"}, "final": None}
    r = await put_settings(client, created["id"], {"assessments": exams})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "trainer_settings_invalid"
    assert detail["problems"][0]["path"] == "assessments.checkIn.formId"
    assert detail["problems"][0]["code"] == "form_not_found"
    assert detail["errors"] == [
        {
            "path": "assessments.check_in.form_id",
            "rule": "unknown_form",
            "message_key": "trainer_settings.unknown_form",
        }
    ]


async def test_a_trainer_without_content_is_a_422(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    r = await put_settings(
        client, created["id"], {"trainer": {"enabled": True, "level": "B1"}}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "pack_not_found"
    assert r.json()["detail"]["errors"] == [
        {
            "path": "trainer.level",
            "rule": "unknown_pack",
            "message_key": "trainer_settings.unknown_pack",
        }
    ]


async def test_enabled_exams_need_at_least_one_policy(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(client, created["id"], {"assessments": {"enabled": True}})
    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "no_exam_configured"
    assert r.json()["detail"]["errors"][0]["rule"] == "no_exam_configured"
    assert r.json()["detail"]["errors"][0]["path"] == "assessments"


def assessment_step(**over: Any) -> dict[str, Any]:
    return {
        "type": "assessment",
        "key": "diagnostic",
        "title": "Diagnostico",
        "purpose": "check_in",
        **over,
    }


async def test_the_assessment_step_needs_an_enabled_check_in(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    welcome = {"steps": [assessment_step()]}
    r = await put_settings(client, created["id"], {"welcome": welcome})
    assert r.status_code == 422
    codes = [p["code"] for p in r.json()["detail"]["problems"]]
    assert codes == ["assessment_step_needs_check_in"]
    errors = r.json()["detail"]["errors"]
    assert errors[0]["rule"] == "assessment_step_needs_check_in"
    assert errors[0]["path"] == "welcome.steps[0]"
    ok = await put_settings(
        client, created["id"], {"welcome": welcome, "assessments": EXAMS}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["settings"]["welcome"]["steps"][0]["purpose"] == "check_in"


async def test_only_one_assessment_step(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    steps = [assessment_step(), assessment_step(key="diagnostic2")]
    r = await put_settings(
        client, created["id"], {"welcome": {"steps": steps}, "assessments": EXAMS}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["problems"][0]["code"] == "assessment_too_many"


async def test_only_the_check_in_purpose_is_a_step(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    steps = [assessment_step(purpose="final")]
    r = await put_settings(
        client, created["id"], {"welcome": {"steps": steps}, "assessments": EXAMS}
    )
    assert r.status_code == 422


# --- the editor's real payload, and content that is not published yet ---------------

# What the editor builds today (trainingForm.ts, `defaultAssessments()` switched
# on): snake_case keys and a form id it guessed.
EDITOR_EXAMS: dict[str, Any] = {
    "enabled": True,
    "check_in": {"form_id": "forma_a", "max_attempts": 1, "time_limit_min": 60},
    "final": {"form_id": "forma_a", "max_attempts": 3, "time_limit_min": 60},
    "pass_percent": 80,
    "pass_operator": "gt",
    "exemption": {"enabled": True},
}


async def test_the_editor_payload_with_a_guessed_form_id_is_refused_by_path(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    body = {"assessments": EDITOR_EXAMS, "welcome": {"steps": [assessment_step()]}}
    r = await put_settings(client, created["id"], body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "trainer_settings_invalid"
    assert [(e["path"], e["rule"]) for e in detail["errors"]] == [
        ("assessments.check_in.form_id", "unknown_form"),
        ("assessments.final.form_id", "unknown_form"),
    ]
    assert "forma_a" not in r.text  # no value of the request is echoed back


def with_form_status(status: str) -> Catalog:
    real = load_catalog(Path("content/trainer"))
    pack = real.pack("en", "A1")
    assert pack is not None
    form = pack.forms["a1-form-a"].model_copy(update={"status": status})
    return Catalog(
        {("en", "A1"): dataclasses.replace(pack, forms={form.form_id: form})}
    )


@pytest.fixture
def form_status() -> Iterator[Any]:
    def use(status: str) -> None:
        app.dependency_overrides[get_catalog] = lambda: with_form_status(status)

    yield use
    app.dependency_overrides.pop(get_catalog, None)


@pytest.mark.parametrize("status", ["draft", "reviewed"])
async def test_a_form_that_is_not_published_can_be_configured_with_a_warning(
    client: httpx.AsyncClient, form_status: Any, status: str
) -> None:
    form_status(status)
    created = await make_map(client)
    r = await put_settings(client, created["id"], {"assessments": EXAMS})
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == [
        {
            "path": "assessments.check_in.form_id",
            "rule": "content_not_published",
            "message_key": "trainer_settings.content_not_published",
        },
        {
            "path": "assessments.final.form_id",
            "rule": "content_not_published",
            "message_key": "trainer_settings.content_not_published",
        },
    ]
    stored = (await full_map(client, created["id"]))["settings"]
    assert stored["assessments"]["checkIn"]["formId"] == "a1-form-a"


async def test_a_published_form_saves_without_warnings(
    client: httpx.AsyncClient, form_status: Any
) -> None:
    form_status("published")
    created = await make_map(client)
    r = await put_settings(client, created["id"], {"assessments": EXAMS})
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []


async def test_a_trainer_whose_content_is_all_draft_warns_at_the_level(
    client: httpx.AsyncClient, draft_a1: None
) -> None:
    created = await make_map(client)
    r = await put_settings(
        client, created["id"], {"trainer": {"enabled": True, "level": "A1"}}
    )
    assert r.status_code == 200, r.text
    assert [(w["path"], w["rule"]) for w in r.json()["warnings"]] == [
        ("trainer.level", "content_not_published")
    ]


async def test_a_save_that_names_no_content_has_no_warnings(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(client, created["id"], {})
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []
