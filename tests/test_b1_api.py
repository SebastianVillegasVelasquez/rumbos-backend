# ruff: noqa: F811
"""The B1 pack through the existing endpoints, with no change to the contract:
the editors' forms list, saving a map for level B1, the practice catalog and
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

PACK = load_pack(Path("content/trainer/en/B1"))
FORM = PACK.forms["b1-form-a"]
B1 = {
    "trainer": {"enabled": True, "language": "en", "level": "B1"},
    "assessments": exams(
        checkIn={"formId": "b1-form-a", "maxAttempts": 1, "timeLimitMin": 90},
        final=None,
    ),
}


async def test_the_editor_forms_list_has_the_b1_form_as_published(
    env: dict[str, Any],
) -> None:
    ed = await env["editor"]()
    forms = {f["id"]: f for f in (await ed.get("/trainer/assessments/forms")).json()}
    assert forms["b1-form-a"] == {
        "id": "b1-form-a",
        "title": "Inglés B1 · Evaluación diagnóstica · Forma A · Diagnóstico de entrada",
        "language": "en",
        "level": "B1",
        "purpose": "check_in",
        "publication_state": "published",
    }
    assert "a1-form-a" in forms


async def test_a_map_for_level_b1_saves_without_content_warnings(
    env: dict[str, Any],
) -> None:
    editor = env["client"]()
    created = await make_map(editor, course=COURSE)
    r = await put_settings(editor, created["id"], B1)
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == []
    await publish(editor, created["id"])


async def test_an_editor_sees_the_whole_b1_catalog_as_published(
    env: dict[str, Any],
) -> None:
    await env["setup"](B1)
    ed = await env["editor"]()
    catalog = (await ed.get("/trainer/catalog")).json()
    assert catalog["enabled"]
    assert (catalog["language"], catalog["level"]) == ("en", "B1")
    assert [m["key"] for m in catalog["modules"]] == ["M1", "M2", "M3", "M4"]
    lessons = [le for m in catalog["modules"] for le in m["lessons"]]
    assert len(lessons) == 15 and {le["vocabulary_count"] for le in lessons} == {8}
    activities = [a for le in lessons for a in le["activities"]]
    assert len(activities) == 45
    assert {a["publication_state"] for a in activities} == {"published"}


async def test_a_student_sees_the_published_b1_without_draft_serving(
    env: dict[str, Any],
) -> None:
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": False}
    )
    await env["setup"](B1)
    s = await env["student"]()
    catalog = (await s.get("/trainer/catalog")).json()
    assert catalog["enabled"] and not catalog["preview"]
    assert (catalog["language"], catalog["level"]) == ("en", "B1")
    listed = (await s.get("/trainer/assessments")).json()["items"][0]
    assert listed["state"] != "locked"
    assert (await s.post("/trainer/assessments/check_in/attempts")).status_code == 201


async def test_a_student_takes_the_b1_check_in_through_the_same_endpoints(
    env: dict[str, Any],
) -> None:
    await env["setup"](B1)
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
    assert served.count("/api/v1/trainer/audio/b1fa-") == 15
    speaking = (await s.get(f"/trainer/attempts/{attempt_id}/sections/speaking")).json()
    (task,) = speaking["parts"][0]["items"]
    assert [q["prompt"] for q in task["questions"]] == [
        "Write what you would say here."
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
