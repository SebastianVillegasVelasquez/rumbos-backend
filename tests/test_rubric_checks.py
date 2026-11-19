"""The two checks added for the A2 rubrics: past-tense verbs and be + -ing."""

from fractions import Fraction

import pytest

from app.assessment.rubric import Response, Rubric, rubric_from, score
from app.trainer.language import ENGLISH

IRREGULAR = ["went", "saw", "ate", "had"]


def _rubric(check: dict[str, object]) -> Rubric:
    return rubric_from(
        {"id": "r", "criteria": [{"id": "c", "points": 4, "checks": [check]}]}
    )


def _points(rubric: Rubric, values: dict[str, str]) -> Fraction:
    text = " ".join(values.values())
    return score(rubric, Response(values, text), frozenset(), ENGLISH).points


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("we walked and we visited a farm and we ate lunch", Fraction(4)),
        ("we walked and we went home", Fraction(8, 3)),
        ("we walk and we eat", Fraction(0)),
        ("", Fraction(0)),
        ("a red bed", Fraction(0)),
    ],
)
def test_past_verbs_count_regular_and_listed_irregular_forms(
    answer: str, expected: Fraction
) -> None:
    rubric = _rubric(
        {"check": "past_verbs_at_least", "on": ["b1"], "min": 3, "phrases": IRREGULAR}
    )
    assert _points(rubric, {"b1": answer}) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("The technician is fixing the heater", Fraction(4)),
        ("He's repairing it", Fraction(4)),
        ("I am working now", Fraction(4)),
        ("The technician fixes the heater", Fraction(0)),
        ("He is king of the ring", Fraction(0)),
        ("fixing is", Fraction(0)),
    ],
)
def test_be_plus_ing_needs_a_form_of_be_and_a_participle(
    answer: str, expected: Fraction
) -> None:
    rubric = _rubric({"check": "be_plus_ing", "on": ["q2"]})
    assert _points(rubric, {"q2": answer}) == expected
