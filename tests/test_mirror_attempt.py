"""The attempt record of the Moodle mirror: it reproduces the backend percentage
exactly from the definition, keeps the strict pass rule at the boundary, and
obeys the size cap and the written-text switch."""

from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.assessment.engine import (
    FormRuntime,
    clip_id,
    option_id,
    runtime_for,
    score_form,
)
from app.assessment.rubric import blank_ids
from app.assessment.scoring import passes
from app.mirror.attempt import MAX_STATEMENT_BYTES, attempt_claims
from app.mirror.definition import build_definition, canonical, definition_hash
from app.trainer.assessment_form import (
    AssessmentForm,
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
)
from app.trainer.language import ENGLISH
from app.trainer.pack import load_pack

ROOT = Path(__file__).resolve().parent.parent / "content/trainer/en"
BASE = {"jti": "00000000-0000-4000-8000-000000000001", "sub": "student1"}
Responses = dict[str, dict[str, Any]]


def pack_runtime(level: str, form_id: str) -> FormRuntime:
    return runtime_for(load_pack(ROOT / level), form_id, ENGLISH)


def wrong_option(item: ChoiceAssessmentItem) -> str:
    return next(o.id for o in item.options if o.id != item.key)


def responses_for(
    form: AssessmentForm, *, right: bool, text: str = "Hello world. I live here."
) -> Responses:
    out: Responses = {}
    for section in form.sections:
        for item in section.items():
            if isinstance(item, ChoiceAssessmentItem):
                chosen = item.key if right else wrong_option(item)
                out[item.item_id] = {
                    "option_id": option_id(form.form_id, item.item_id, chosen)
                }
            elif isinstance(item, MatchAudioItem):
                pairs = [
                    [clip_id(form.form_id, item.item_id, i), place]
                    for i, place in item.key
                ]
                if not right:
                    pairs = [[c, pairs[0][1]] for c, _ in pairs]
                out[item.item_id] = {"pairs": pairs}
            else:
                assert isinstance(item, ProductiveItem)
                ids = [f.id for f in item.fields] or [q.id for q in item.questions]
                if item.template:
                    ids = blank_ids(item.template)
                out[item.item_id] = {"values": {i: text for i in ids}}
    return out


def mirror_percent(
    definition: Mapping[str, Any], claims: Mapping[str, Any]
) -> Fraction:
    """What the plugin does with the two payloads: the weights come from the
    definition, the points from the attempt. No other input."""
    weight = {
        q["question_id"]: Fraction(q["weight_points"]) / q["max_points"]
        for s in definition["sections"]
        for p in s["parts"]
        for q in p["questions"]
    }
    total = Fraction(0)
    for entry in [*claims["answers"], *claims["written"]]:
        total += Fraction(entry["points_exact"]) * weight[entry["question_id"]]
    return total


def claims_of(
    runtime: FormRuntime,
    responses: Responses,
    *,
    include_text: bool = True,
    max_bytes: int = MAX_STATEMENT_BYTES,
) -> tuple[dict[str, Any], dict[str, Any], Fraction]:
    form = runtime.form
    definition = build_definition(form)
    scored = score_form(runtime, responses, ENGLISH)
    claims = attempt_claims(
        BASE,
        form=form,
        definition_hash=definition_hash(definition),
        status="submitted",
        responses=responses,
        scored=scored,
        include_written_text=include_text,
        max_bytes=max_bytes,
    )
    return definition, claims, scored.total.percent


@pytest.mark.parametrize(
    ("level", "form_id"), [("A2", "a2-form-a"), ("B2", "b2-form-a")]
)
@pytest.mark.parametrize("mode", ["right", "wrong", "empty", "mixed"])
def test_the_mirror_reproduces_the_backend_percentage(
    level: str, form_id: str, mode: str
) -> None:
    runtime = pack_runtime(level, form_id)
    form = runtime.form
    if mode == "empty":
        responses: Responses = {}
    elif mode == "mixed":
        good = responses_for(form, right=True)
        bad = responses_for(form, right=False)
        responses = {k: (good if i % 2 else bad)[k] for i, k in enumerate(good)}
    else:
        responses = responses_for(form, right=mode == "right")
    definition, claims, percent = claims_of(runtime, responses)
    assert mirror_percent(definition, claims) == percent
    assert Fraction(claims["percent_exact"]) == percent
    assert claims["definition_hash"] == definition_hash(definition)


def test_every_question_is_in_the_record_even_when_unanswered() -> None:
    runtime = pack_runtime("B2", "b2-form-a")
    definition, claims, _ = claims_of(runtime, {})
    ids = [
        q["question_id"]
        for s in definition["sections"]
        for p in s["parts"]
        for q in p["questions"]
    ]
    recorded = [e["question_id"] for e in [*claims["answers"], *claims["written"]]]
    assert sorted(recorded) == sorted(ids)
    assert all(a["answer"] is None and not a["correct"] for a in claims["answers"])
    assert all(a["points_exact"] == "0" for a in claims["answers"])


def test_the_answer_is_the_source_option_not_the_opaque_one_the_student_saw() -> None:
    runtime = pack_runtime("A2", "a2-form-a")
    definition, claims, _ = claims_of(runtime, responses_for(runtime.form, right=True))
    keys = {
        q["question_id"]: q["correct"]
        for s in definition["sections"]
        for p in s["parts"]
        for q in p["questions"]
        if "correct" in q
    }
    for answer in claims["answers"]:
        assert answer["answer"] == keys[answer["question_id"]]
        assert answer["correct"] is True


def test_a_matching_item_is_recorded_pair_by_pair() -> None:
    runtime = pack_runtime("B2", "b2-form-a")
    _, claims, _ = claims_of(runtime, responses_for(runtime.form, right=False))
    pairs = [a for a in claims["answers"] if ":" in a["question_id"]]
    assert len(pairs) == 5
    # Every clip was put in the first place: only the pair that belongs there
    # is right.
    assert sum(a["correct"] for a in pairs) == 1


# --- the pass rule at the boundary -----------------------------------------------------


def synthetic() -> FormRuntime:
    """Two sections of ten items, worth 4.9 and 5.1 points of the 100 each."""

    def section(key: str, weight: int) -> dict[str, Any]:
        items = [
            {
                "type": "mcq",
                "item_id": f"{key}.q{n}",
                "prompt": f"Question {n}",
                "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                "key": "a",
            }
            for n in range(10)
        ]
        part = {"part_key": f"{key}-1", "title": "P", "instructions": "I"}
        return {
            "key": key,
            "title": key,
            "weight": weight,
            "parts": [{**part, "items": items}],
        }

    form = AssessmentForm.model_validate(
        {
            "form_id": "synthetic",
            "title": "T",
            "language": "en",
            "level": "A1",
            "status": "published",
            "sections": [section("listening", 49), section("reading", 51)],
        }
    )
    return FormRuntime(form, {}, frozenset())


def synthetic_responses(listening: int, reading: int) -> Responses:
    out: Responses = {}
    for key, right in (("listening", listening), ("reading", reading)):
        for n in range(10):
            item_id = f"{key}.q{n}"
            chosen = "a" if n < right else "b"
            out[item_id] = {"option_id": option_id("synthetic", item_id, chosen)}
    return out


@pytest.mark.parametrize(
    ("listening", "reading", "percent", "passes_strictly"),
    [
        (8, 8, Fraction(80), False),  # exactly at the mark: not above it
        (7, 9, Fraction(802, 10), True),  # 80.2
        (9, 7, Fraction(798, 10), False),  # 79.8
        (10, 10, Fraction(100), True),
        (0, 0, Fraction(0), False),
    ],
)
def test_the_boundary_values_decide_strictly_above(
    listening: int, reading: int, percent: Fraction, passes_strictly: bool
) -> None:
    runtime = synthetic()
    definition, claims, backend = claims_of(
        runtime, synthetic_responses(listening, reading)
    )
    assert backend == percent
    assert mirror_percent(definition, claims) == percent
    assert passes(backend, 80, "gt") is passes_strictly
    # `gte` would pass the exact 80: the mirror must take the backend's verdict,
    # never Moodle's own "grade to pass".
    assert passes(backend, 80, "gte") is (passes_strictly or percent == 80)


def test_a_form_edited_after_publication_has_a_new_hash_and_old_claims_stay() -> None:
    runtime = synthetic()
    responses = synthetic_responses(8, 8)
    definition, before, _ = claims_of(runtime, responses)
    edited = build_definition(runtime.form)
    edited["sections"][0]["parts"][0]["questions"][0]["prompt"] = "Changed"
    assert definition_hash(edited) != before["definition_hash"]
    assert definition_hash(definition) == before["definition_hash"]
    _, again, _ = claims_of(runtime, responses)
    assert canonical(again) == canonical(before)


# --- size and the written-text switch ---------------------------------------------------


def test_written_text_is_sent_only_when_the_tenant_allows_it() -> None:
    runtime = pack_runtime("B2", "b2-form-a")
    responses = responses_for(runtime.form, right=True, text="Some words here.")
    _, on, _ = claims_of(runtime, responses, include_text=True)
    _, off, _ = claims_of(runtime, responses, include_text=False)
    assert on["written_text_included"] is True
    assert off["written_text_included"] is False
    assert all(w["text"] for w in on["written"])
    assert all(w["text"] is None for w in off["written"])
    assert [w["points_exact"] for w in on["written"]] == [
        w["points_exact"] for w in off["written"]
    ]
    assert on["truncated"] is False and off["truncated"] is False
    assert "Some words here." not in canonical(off).decode()


def test_over_the_cap_the_text_goes_and_no_answer_is_dropped() -> None:
    runtime = pack_runtime("B2", "b2-form-a")
    responses = responses_for(runtime.form, right=True, text="word " * 200)
    _, full, _ = claims_of(runtime, responses)
    _, cut, _ = claims_of(runtime, responses, max_bytes=len(canonical(full)) - 1)
    assert cut["truncated"] is True and cut["written_text_included"] is False
    assert all(w["text"] is None for w in cut["written"])
    assert cut["answers"] == full["answers"]
    assert [w["points_exact"] for w in cut["written"]] == [
        w["points_exact"] for w in full["written"]
    ]


def test_the_largest_exam_fits_the_cap_with_room_to_spare() -> None:
    """Every written answer at its maximum, in four-byte characters."""
    runtime = pack_runtime("B2", "b2-form-a")
    heavy = "\U0001f600" * 1500
    responses = responses_for(runtime.form, right=True, text=heavy)
    _, claims, _ = claims_of(runtime, responses)
    assert claims["truncated"] is False
    assert len(canonical(claims)) < MAX_STATEMENT_BYTES // 2
