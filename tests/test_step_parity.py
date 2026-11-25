"""One validator table for both flows: every content step type is allowed in
the welcome and the closing flow, the greeting is welcome-only, the exam step
is `check_in` in one and `final` in the other, and the OpenAPI says so."""

import uuid
from typing import Any

import pytest

from app.main import app
from app.schemas.welcome import ClosingFlow, Welcome
from app.welcome_flow.flow import flow_problems

KEY = "abcdef12"
STEPS: dict[str, dict[str, Any]] = {
    "video": {
        "type": "video",
        "key": "video001",
        "title": "V",
        "video": {"provider": "youtube", "videoId": "dQw4w9WgXcQ"},
    },
    "html": {"type": "html", "key": "html0001", "title": "H", "html": "<p>x</p>"},
    "pdf": {
        "type": "pdf",
        "key": "pdf00001",
        "title": "P",
        "assetId": str(uuid.uuid4()),
    },
    "quiz": {"type": "quiz", "key": "quiz0001", "title": "Q", "cmid": 5},
    "moodle_activity": {
        "type": "moodle_activity",
        "key": "actividad",
        "title": "A",
        "cmid": 6,
    },
}
PURPOSE = {"welcome": "check_in", "closing": "final"}


def codes(flow: str, step: dict[str, Any]) -> list[str]:
    model = ClosingFlow if flow == "closing" else Welcome
    parsed = model.model_validate({"steps": [step]})
    return [p["code"] for p in flow_problems(parsed, name=flow)]  # type: ignore[arg-type]


@pytest.mark.parametrize("flow", ["welcome", "closing"])
@pytest.mark.parametrize("kind", list(STEPS))
def test_every_shared_content_type_is_allowed_in_both_flows(
    flow: str, kind: str
) -> None:
    assert codes(flow, STEPS[kind]) == []


@pytest.mark.parametrize("flow", ["welcome", "closing"])
def test_the_exam_step_has_the_purpose_of_its_flow(flow: str) -> None:
    other = "final" if flow == "welcome" else "check_in"
    exam = {"type": "assessment", "key": "examen01", "title": "E"}
    assert codes(flow, {**exam, "purpose": PURPOSE[flow]}) == []
    assert codes(flow, {**exam, "purpose": other}) == ["assessment_purpose"]


def test_the_greeting_is_welcome_only() -> None:
    greeting = {"type": "welcome", "key": "saludo01", "title": "Hola"}
    assert codes("welcome", greeting) == []
    assert codes("closing", greeting) == ["step_type_not_allowed"]


@pytest.mark.parametrize("flow", ["welcome", "closing"])
def test_a_step_key_may_not_repeat_the_other_flows(flow: str) -> None:
    model = ClosingFlow if flow == "closing" else Welcome
    parsed = model.model_validate({"steps": [STEPS["html"]]})
    found = flow_problems(parsed, name=flow, other_keys={"html0001"})  # type: ignore[arg-type]
    assert [p["code"] for p in found] == ["step_key_duplicate"]


def test_maps_saved_before_load_unchanged() -> None:
    old = {"steps": [STEPS["html"], STEPS["video"]]}
    assert ClosingFlow.model_validate(old).model_dump(
        by_alias=True, mode="json"
    ) == Welcome.model_validate(old).model_dump(by_alias=True, mode="json")


def test_openapi_says_what_the_closing_flow_may_hold() -> None:
    schemas = app.openapi()["components"]["schemas"]
    steps = schemas["ClosingFlow"]["properties"]["steps"]
    items = next(b["items"] for b in steps["anyOf"] if "items" in b)
    refs = [r.get("$ref", "") or r["allOf"][0]["$ref"] for r in items["oneOf"]]
    assert [r.rsplit("/", 1)[1] for r in refs] == [
        "VideoStep",
        "HtmlStep",
        "PdfStep",
        "QuizStep",
        "MoodleActivityStep",
        "AssessmentStep",
    ]
    exam = items["oneOf"][-1]
    assert exam["properties"]["purpose"]["const"] == "final"
    assert "welcome" not in items["discriminator"]["mapping"]
