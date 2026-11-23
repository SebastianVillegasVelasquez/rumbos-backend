"""The exam definition of the Moodle mirror: it covers the whole 100, leaves out
what must not travel, and its hash is the form's identity."""

from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.mirror.definition import (
    build_definition,
    canonical,
    definition_hash,
    units,
)
from app.trainer.assessment_form import AssessmentForm
from app.trainer.pack import load_pack

ROOT = Path(__file__).resolve().parent.parent / "content/trainer/en"
FORMS = [("A1", "a1-form-a"), ("A2", "a2-form-a"), ("B2", "b2-form-a")]


def form_of(level: str, form_id: str) -> AssessmentForm:
    return load_pack(ROOT / level).forms[form_id]


def questions(definition: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        q for s in definition["sections"] for p in s["parts"] for q in p["questions"]
    ]


@pytest.mark.parametrize(("level", "form_id"), FORMS)
def test_the_weights_of_the_questions_add_up_to_one_hundred(
    level: str, form_id: str
) -> None:
    definition = build_definition(form_of(level, form_id))
    total = sum(Fraction(q["weight_points"]) for q in questions(definition))
    assert total == 100


@pytest.mark.parametrize(("level", "form_id"), FORMS)
def test_every_unit_of_the_form_is_one_question(level: str, form_id: str) -> None:
    form = form_of(level, form_id)
    ids = [q["question_id"] for q in questions(build_definition(form))]
    assert ids == [u.question_id for u in units(form)]
    assert len(set(ids)) == len(ids)
    assert sum(u.max_points for u in units(form)) == sum(
        s.max_points() for s in form.sections
    )


def test_the_b2_form_has_45_objective_units_and_three_written_tasks() -> None:
    qs = questions(build_definition(form_of("B2", "b2-form-a")))
    written = [q for q in qs if "written" in q]
    assert len(qs) - len(written) == 45 and len(written) == 3
    assert {q["max_points"] for q in written} == {5}


def test_a_matching_item_is_one_question_per_pair() -> None:
    qs = questions(build_definition(form_of("B2", "b2-form-a")))
    pairs = [q for q in qs if q["type"] == "match_pair"]
    assert len(pairs) == 5
    assert all(":" in q["question_id"] and q["correct"] for q in pairs)
    assert all(q["max_points"] == 1 for q in pairs)


def test_what_must_not_travel_is_not_in_the_definition() -> None:
    form = form_of("B2", "b2-form-a")
    text = canonical(build_definition(form)).decode()
    assert "transcript" not in text and '"rubric"' not in text
    assert "source_rubric" not in text and "source_number" not in text
    for item in (i for s in form.sections for i in s.items()):
        if hasattr(item, "clips"):
            assert all(c.transcript not in text for c in item.clips)


def test_the_hash_is_stable_and_follows_the_content() -> None:
    form = form_of("A2", "a2-form-a")
    first = definition_hash(build_definition(form))
    assert first == definition_hash(build_definition(form))
    assert len(first) == 64
    changed = build_definition(form)
    questions(changed)[0]["prompt"] += "."
    assert definition_hash(changed) != first


def test_the_hash_ignores_the_rest_of_the_pack() -> None:
    """The pack's `content_version` moves with every file; this does not."""
    one = definition_hash(build_definition(form_of("A2", "a2-form-a")))
    again = definition_hash(build_definition(form_of("A2", "a2-form-a")))
    assert one == again
    assert one != definition_hash(build_definition(form_of("B2", "b2-form-a")))
