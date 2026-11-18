"""The closing flow: the welcome flow's twin, shown when the level is finished.
Its settings, every rule of its validators, the final-exam step, and that a map
without one reads, writes and hashes exactly as before."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.main import app
from app.publishing.snapshot import collect_asset_ids
from app.schemas.map_settings import MapSettings
from app.trainer.catalog import Catalog, load_catalog
from app.trainer.dependencies import get_catalog
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.test_publications_api import full_map, make_map
from tests.test_trainer_settings import EXAMS
from tests.test_welcome_api import put_settings


@pytest.fixture
async def client(engine: Any) -> AsyncIterator[httpx.AsyncClient]:
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    async with EditorClient(transport=transport, base_url="http://t/api/v1") as c:
        yield c


def content_step(**over: Any) -> dict[str, Any]:
    return {
        "type": "html",
        "key": "cierre01",
        "title": "Despedida",
        "html": "<h2>Lo lograste</h2><p>Gracias.</p>",
        **over,
    }


def final_step(**over: Any) -> dict[str, Any]:
    return {
        "type": "assessment",
        "key": "examenfin",
        "title": "Evaluacion final",
        "purpose": "final",
        **over,
    }


async def put_closing(
    client: httpx.AsyncClient, closing: Any, **settings: Any
) -> httpx.Response:
    created = await make_map(client)
    return await put_settings(client, created["id"], {"closing": closing, **settings})


def problems_of(r: httpx.Response) -> list[tuple[str, str]]:
    return [(p["path"], p["code"]) for p in r.json()["detail"]["problems"]]


# --- the shape and what a map without one looks like ----------------------------------


def test_no_closing_flow_is_the_default_and_is_not_stored() -> None:
    settings = MapSettings()
    assert settings.closing is None
    assert "closing" not in settings.to_stored()


def test_null_closing_reads_as_none_and_is_not_stored() -> None:
    assert MapSettings.model_validate({"closing": None}).to_stored() == (
        MapSettings().to_stored()
    )


def test_a_map_stored_before_the_closing_flow_keeps_its_stored_form() -> None:
    before = MapSettings.model_validate({"mode": "guided"}).to_stored()
    assert "closing" not in before
    assert MapSettings.model_validate(before).to_stored() == before


def test_a_closing_flow_is_stored_with_its_steps() -> None:
    settings = MapSettings.model_validate({"closing": {"steps": [content_step()]}})
    stored = settings.to_stored()
    assert stored["closing"]["steps"][0]["key"] == "cierre01"
    assert MapSettings.model_validate(stored).to_stored() == stored


async def test_a_map_without_a_closing_flow_reads_and_saves_as_before(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(client, created["id"], {"mode": "guided"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["closing"] is None
    again = await put_settings(client, created["id"], r.json()["settings"])
    assert again.status_code == 200, again.text
    assert again.json()["settings"] == r.json()["settings"]


# --- writing it through the API ------------------------------------------------------


async def test_a_closing_flow_is_saved_and_read_back(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    closing = {"steps": [content_step(), final_step()]}
    r = await put_settings(
        client, created["id"], {"closing": closing, "assessments": EXAMS}
    )
    assert r.status_code == 200, r.text
    steps = (await full_map(client, created["id"]))["settings"]["closing"]["steps"]
    assert [(s["type"], s["key"]) for s in steps] == [
        ("html", "cierre01"),
        ("assessment", "examenfin"),
    ]
    assert steps[1]["purpose"] == "final"


async def test_clearing_the_closing_flow_removes_it(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await put_settings(client, created["id"], {"closing": {"steps": [content_step()]}})
    r = await put_settings(client, created["id"], {"closing": None})
    assert r.status_code == 200, r.text
    assert (await full_map(client, created["id"]))["settings"]["closing"] is None


async def test_the_steps_can_be_reordered(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    steps = [final_step(), content_step()]
    r = await put_settings(
        client, created["id"], {"closing": {"steps": steps}, "assessments": EXAMS}
    )
    assert r.status_code == 200, r.text
    assert [s["key"] for s in r.json()["settings"]["closing"]["steps"]] == [
        "examenfin",
        "cierre01",
    ]


async def test_video_and_pdf_steps_are_content(client: httpx.AsyncClient) -> None:
    created = await make_map(client)
    video = {
        "type": "video",
        "key": "video001",
        "title": "Video",
        "video": {"provider": "youtube", "videoId": "dQw4w9WgXcQ"},
    }
    r = await put_settings(
        client, created["id"], {"closing": {"steps": [video, content_step()]}}
    )
    assert r.status_code == 200, r.text


# --- every rule of the flow ------------------------------------------------------------


async def test_a_closing_flow_needs_steps(client: httpx.AsyncClient) -> None:
    r = await put_closing(client, {})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "welcome_invalid"
    assert problems_of(r) == [("closing", "closing_needs_steps")]


async def test_the_legacy_fields_are_refused_next_to_steps(
    client: httpx.AsyncClient,
) -> None:
    r = await put_closing(client, {"title": "Fin", "steps": [content_step()]})
    assert r.status_code == 422
    assert ("closing.title", "legacy_fields_with_steps") in problems_of(r)


async def test_the_greeting_is_not_a_closing_step(client: httpx.AsyncClient) -> None:
    greeting = {"type": "welcome", "key": "saludo01", "title": "Hola"}
    r = await put_closing(client, {"steps": [greeting]})
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0]", "step_type_not_allowed")]


async def test_a_moodle_quiz_is_not_a_closing_step(client: httpx.AsyncClient) -> None:
    quiz = {"type": "quiz", "key": "cuestion1", "title": "Quiz", "cmid": 7}
    r = await put_closing(client, {"steps": [quiz]})
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0]", "step_type_not_allowed")]


async def test_the_closing_exam_must_be_the_final_one(
    client: httpx.AsyncClient,
) -> None:
    r = await put_closing(
        client,
        {"steps": [final_step(purpose="check_in")]},
        assessments=EXAMS,
    )
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0].purpose", "assessment_purpose")]


async def test_the_welcome_exam_must_be_the_check_in_one(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    r = await put_settings(
        client,
        created["id"],
        {"welcome": {"steps": [final_step()]}, "assessments": EXAMS},
    )
    assert r.status_code == 422
    assert problems_of(r) == [("welcome.steps[0].purpose", "assessment_purpose")]


async def test_only_one_final_exam_step(client: httpx.AsyncClient) -> None:
    steps = [final_step(), final_step(key="examenfin2")]
    r = await put_closing(client, {"steps": steps}, assessments=EXAMS)
    assert r.status_code == 422
    assert ("closing.steps[1]", "assessment_too_many") in problems_of(r)


async def test_a_step_key_is_unique_within_the_flow(
    client: httpx.AsyncClient,
) -> None:
    steps = [content_step(), content_step()]
    r = await put_closing(client, {"steps": steps})
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[1].key", "step_key_duplicate")]


async def test_a_step_key_is_unique_across_both_flows(
    client: httpx.AsyncClient,
) -> None:
    """Moodle keeps the progress of a step by map and key alone."""
    created = await make_map(client)
    welcome = {"steps": [content_step(key="compartida")]}
    closing = {"steps": [content_step(key="compartida")]}
    r = await put_settings(
        client, created["id"], {"welcome": welcome, "closing": closing}
    )
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0].key", "step_key_duplicate")]


async def test_forbidden_markup_is_refused_in_the_closing_flow(
    client: httpx.AsyncClient,
) -> None:
    step = content_step(html="<script>alert(1)</script>")
    r = await put_closing(client, {"steps": [step]})
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0].html", "html_forbidden_markup")]
    assert "alert(1)" not in r.text


async def test_forbidden_css_is_refused_in_the_closing_flow(
    client: httpx.AsyncClient,
) -> None:
    step = content_step(css="body { background: url(http://evil.test/x.png) }")
    r = await put_closing(client, {"steps": [step]})
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0].css", "css_forbidden")]


async def test_a_document_that_is_not_available_is_refused(
    client: httpx.AsyncClient,
) -> None:
    pdf = {
        "type": "pdf",
        "key": "documento",
        "title": "Certificado",
        "assetId": str(uuid.uuid4()),
    }
    r = await put_closing(client, {"steps": [pdf]})
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0].assetId", "document_unavailable")]


async def test_type_errors_are_reported_under_closing(
    client: httpx.AsyncClient,
) -> None:
    unknown = {"type": "carousel", "key": "carrusel1", "title": "x"}
    r = await put_closing(client, {"steps": [unknown]})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "welcome_invalid"
    assert problems_of(r) == [("closing.steps[0]", "step_type_invalid")]

    r = await put_closing(client, {"steps": [final_step(purpose="midterm")]})
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps[0].purpose", "invalid_value")]


async def test_too_many_steps_are_refused(client: httpx.AsyncClient) -> None:
    steps = [content_step(key=f"paso{n:03d}") for n in range(13)]
    r = await put_closing(client, {"steps": steps})
    assert r.status_code == 422
    assert problems_of(r) == [("closing.steps", "steps_too_many")]


async def test_an_invalid_closing_flow_leaves_the_appearance_untouched(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    await put_settings(client, created["id"], {"mode": "guided"})
    r = await put_settings(
        client, created["id"], {"mode": "explorative", "closing": {}}
    )
    assert r.status_code == 422
    assert (await full_map(client, created["id"]))["settings"]["mode"] == "guided"


# --- the final exam step needs the exam -------------------------------------------------


async def test_the_final_step_needs_the_exams_enabled(
    client: httpx.AsyncClient,
) -> None:
    r = await put_closing(client, {"steps": [final_step()]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "trainer_settings_invalid"
    assert detail["errors"] == [
        {
            "path": "closing.steps[0]",
            "rule": "assessment_step_needs_final",
            "message_key": "trainer_settings.assessment_step_needs_final",
        }
    ]


async def test_the_final_step_needs_a_final_exam_not_just_a_check_in(
    client: httpx.AsyncClient,
) -> None:
    exams = {**EXAMS, "final": None}
    r = await put_closing(client, {"steps": [final_step()]}, assessments=exams)
    assert r.status_code == 422
    assert [e["rule"] for e in r.json()["detail"]["errors"]] == [
        "assessment_step_needs_final"
    ]


async def test_the_final_step_is_fine_with_the_final_exam(
    client: httpx.AsyncClient,
) -> None:
    r = await put_closing(client, {"steps": [final_step()]}, assessments=EXAMS)
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


async def test_an_unknown_final_form_is_a_422_by_path(
    client: httpx.AsyncClient,
) -> None:
    exams = {**EXAMS, "final": {"formId": "no-such-form"}}
    r = await put_closing(client, {"steps": [final_step()]}, assessments=exams)
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == [
        {
            "path": "assessments.final.form_id",
            "rule": "unknown_form",
            "message_key": "trainer_settings.unknown_form",
        }
    ]
    assert "no-such-form" not in r.text


def with_form_status(status: str) -> Catalog:
    real = load_catalog(Path("content/trainer"))
    pack = real.pack("en", "A1")
    assert pack is not None
    form = pack.forms["a1-form-a"].model_copy(update={"status": status})
    return Catalog(
        {("en", "A1"): dataclasses.replace(pack, forms={form.form_id: form})}
    )


@pytest.fixture
def published_form() -> Iterator[None]:
    app.dependency_overrides[get_catalog] = lambda: with_form_status("published")
    yield
    app.dependency_overrides.pop(get_catalog, None)


async def test_a_published_final_form_saves_without_warnings(
    client: httpx.AsyncClient, published_form: None
) -> None:
    r = await put_closing(client, {"steps": [final_step()]}, assessments=EXAMS)
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []


# --- assets and publication ---------------------------------------------------------------


def test_a_snapshot_depends_on_the_assets_of_its_closing_flow() -> None:
    asset = uuid.uuid4()
    pdf = {"type": "pdf", "assetId": str(asset)}
    snapshot: dict[str, Any] = {
        "imageUrl": "",
        "skins": {},
        "bubbles": [],
        "settings": {"closing": {"steps": [pdf]}},
    }
    assert collect_asset_ids(snapshot) == [asset]
    snapshot["settings"] = {}
    assert collect_asset_ids(snapshot) == []
