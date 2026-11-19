# ruff: noqa: F811
"""The A2 pack through the existing endpoints, with no change to the contract:
the editors' forms list, saving a map for level A2, the practice catalog and
the check-in exam, for editors and students."""

import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.main import app
from app.trainer.pack import load_pack
from tests.test_a2_form import _right_answers
from tests.test_assessment_api import (  # noqa: F401
    COURSE,
    FakeClock,
    clock,
    draft_content_is_served,
    env,
    exams,
    start,
)
from tests.test_publications_api import make_map, publish
from tests.test_welcome_api import put_settings

PACK = load_pack(Path("content/trainer/en/A2"))
FORM = PACK.forms["a2-form-a"]
A2 = {
    "trainer": {"enabled": True, "language": "en", "level": "A2"},
    "assessments": exams(
        checkIn={"formId": "a2-form-a", "maxAttempts": 1, "timeLimitMin": 90},
        final=None,
    ),
}


async def test_the_editor_forms_list_has_the_a2_form_as_a_draft(
    env: dict[str, Any],
) -> None:
    ed = await env["editor"]()
    forms = {f["id"]: f for f in (await ed.get("/trainer/assessments/forms")).json()}
    assert forms["a2-form-a"] == {
        "id": "a2-form-a",
        "title": "Inglés A2 · Evaluación diagnóstica · Forma A · Diagnóstico de entrada",
        "publication_state": "draft",
    }
    assert "a1-form-a" in forms


async def test_a_map_for_level_a2_saves_with_content_warnings(
    env: dict[str, Any],
) -> None:
    editor = env["client"]()
    created = await make_map(editor, course=COURSE)
    r = await put_settings(editor, created["id"], A2)
    assert r.status_code == 200, r.text
    warnings = {(w["path"], w["rule"]) for w in r.json()["warnings"]}
    assert warnings == {
        ("trainer.level", "content_not_published"),
        ("assessments.check_in.form_id", "content_not_published"),
    }
    await publish(editor, created["id"])


async def test_an_editor_in_preview_sees_the_whole_a2_catalog_as_draft(
    env: dict[str, Any],
) -> None:
    await env["setup"](A2)
    ed = await env["editor"]()
    catalog = (await ed.get("/trainer/catalog")).json()
    assert catalog["enabled"] and catalog["preview"]
    assert (catalog["language"], catalog["level"]) == ("en", "A2")
    assert [m["key"] for m in catalog["modules"]] == ["M1", "M2", "M3", "M4"]
    lessons = [le for m in catalog["modules"] for le in m["lessons"]]
    assert len(lessons) == 13 and {le["vocabulary_count"] for le in lessons} == {8}
    activities = [a for le in lessons for a in le["activities"]]
    assert len(activities) == 39
    assert {a["publication_state"] for a in activities} == {"draft"}


async def test_a_student_sees_nothing_of_a2_while_it_is_not_published(
    env: dict[str, Any],
) -> None:
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": False}
    )
    await env["setup"](A2)
    s = await env["student"]()
    assert (await s.get("/trainer/catalog")).json() == {
        "enabled": False,
        "reason": "no_published_content",
        "preview": False,
    }
    listed = (await s.get("/trainer/assessments")).json()["items"][0]
    assert listed["state"] == "locked" and "publication_state" not in listed
    assert (await s.post("/trainer/assessments/check_in/attempts")).status_code == 404


async def test_a_student_takes_the_a2_check_in_through_the_same_endpoints(
    env: dict[str, Any],
) -> None:
    await env["setup"](A2)
    s = await env["student"]()
    listed = (await s.get("/trainer/assessments")).json()["items"][0]
    assert (listed["section_count"], listed["item_count"]) == (5, 44)
    attempt = await start(s)
    assert [x["key"] for x in attempt["sections"]] == [
        "listening",
        "reading",
        "use_of_english",
        "writing",
        "speaking",
    ]
    assert [x["item_count"] for x in attempt["sections"]] == [11, 15, 15, 2, 1]
    attempt_id = attempt["attempt_id"]
    served = (await s.get(f"/trainer/attempts/{attempt_id}/sections/listening")).text
    assert "transcript" not in served and "source_number" not in served
    assert served.count("/api/v1/trainer/audio/a2fa-") == 15
    speaking = (await s.get(f"/trainer/attempts/{attempt_id}/sections/speaking")).json()
    (task,) = speaking["parts"][0]["items"]
    assert [q["prompt"] for q in task["questions"]] == [
        "What are you going to do tomorrow?",
        "What is the technician doing now?",
    ]
    answers = [{"item_id": k, "response": v} for k, v in _right_answers(FORM).items()]
    r = await s.put(
        f"/trainer/attempts/{attempt_id}/answers", json={"answers": answers}
    )
    assert r.status_code == 200, r.text
    result = (await s.post(f"/trainer/attempts/{attempt_id}/submit")).json()
    assert (result["percent"], result["passed"], result["band"]) == (
        "70.00",
        False,
        "consolidating",
    )
    by_section = {x["key"]: x for x in result["by_section"]}
    assert (by_section["listening"]["points"], by_section["listening"]["max"]) == (
        "15.00",
        15,
    )
    assert (by_section["speaking"]["max"], by_section["speaking"]["weight"]) == (5, 15)
    assert "delivery" not in json.dumps(result)
