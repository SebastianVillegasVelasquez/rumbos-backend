from fractions import Fraction
from itertools import product

import pytest

from app.assessment.scoring import (
    SectionScore,
    band_of,
    decimal_string,
    module_percents,
    passes,
    total_of,
)

WEIGHTS = {
    "listening": 25,
    "reading": 25,
    "use_of_english": 20,
    "writing": 15,
    "speaking": 15,
}
MAXES = {
    "listening": 15,
    "reading": 15,
    "use_of_english": 15,
    "writing": 10,
    "speaking": 10,
}


def sections(*points: int | Fraction) -> list[SectionScore]:
    return [
        SectionScore(key, Fraction(p), MAXES[key], WEIGHTS[key])
        for key, p in zip(WEIGHTS, points, strict=True)
    ]


def test_all_correct_and_all_wrong() -> None:
    assert total_of(sections(15, 15, 15, 10, 10)).percent == 100
    assert total_of(sections(0, 0, 0, 0, 0)).percent == 0


def test_the_weights_apply() -> None:
    assert total_of(sections(15, 0, 0, 0, 0)).percent == 25
    assert total_of(sections(0, 0, 15, 0, 0)).percent == 20
    assert total_of(sections(0, 0, 0, 10, 0)).percent == 15
    assert total_of(sections(15, 15, 0, 0, 10)).percent == 65


def test_section_percent_is_points_over_max() -> None:
    s = SectionScore("listening", Fraction(12), 15, 25)
    assert s.percent == 80


@pytest.mark.parametrize(
    ("percent", "operator", "expected"),
    [
        (Fraction(80), "gt", False),
        (Fraction(80), "gte", True),
        (Fraction(8001, 100), "gt", True),
        (Fraction(8001, 100), "gte", True),
        (Fraction(7999, 100), "gt", False),
        (Fraction(7999, 100), "gte", False),
        (Fraction(100), "gt", True),
        (Fraction(0), "gte", False),
    ],
)
def test_pass_rule_boundaries(percent: Fraction, operator: str, expected: bool) -> None:
    assert passes(percent, 80, operator) is expected  # type: ignore[arg-type]


def test_a_value_that_rounds_to_the_mark_does_not_pass_gt_unless_it_is_above() -> None:
    just_above = Fraction(80) + Fraction(1, 10_000)
    assert decimal_string(just_above) == "80.00"
    assert passes(just_above, 80, "gt") is True  # exact value decides, not display
    just_below = Fraction(80) - Fraction(1, 10_000)
    assert decimal_string(just_below) == "80.00"
    assert passes(just_below, 80, "gte") is False


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Fraction(0), "0.00"),
        (Fraction(100), "100.00"),
        (Fraction(1, 6), "0.17"),
        (Fraction(1, 3), "0.33"),
        (Fraction(1, 200), "0.01"),  # half up
        (Fraction(1999, 2400), "0.83"),
        (Fraction(83_25, 100), "83.25"),
    ],
)
def test_decimal_string(value: Fraction, text: str) -> None:
    assert decimal_string(value) == text


@pytest.mark.parametrize(
    ("percent", "band"),
    [
        (Fraction(0), "emerging"),
        (Fraction(39) + Fraction(99, 100), "emerging"),
        (Fraction(40), "developing"),
        (Fraction(59), "developing"),
        (Fraction(60), "consolidating"),
        (Fraction(79), "consolidating"),
        (Fraction(80), "achieved"),
        (Fraction(100), "achieved"),
    ],
)
def test_bands(percent: Fraction, band: str) -> None:
    assert band_of(percent) == band


def test_exactly_80_is_achieved_but_does_not_pass_gt() -> None:
    assert band_of(Fraction(80)) == "achieved"
    assert passes(Fraction(80), 80, "gt") is False


def test_every_reachable_total_is_a_multiple_of_a_sixth() -> None:
    """So the displayed 2 decimals never hide a value on the wrong side of 80,
    and the smallest total above 80 is 80.17."""
    above: list[Fraction] = []
    reached_80 = False
    for l, r, u, w, s in product(
        range(16), range(16), range(16), (0, 5, 10), (0, 5, 10)
    ):
        total = total_of(sections(l, r, u, w, s)).percent
        assert (total * 6).denominator == 1
        if total == 80:
            reached_80 = True
        if total > 80:
            above.append(total)
    assert reached_80
    assert decimal_string(min(above)) == "80.17"


def test_module_percents_skips_modules_with_nothing_to_score() -> None:
    result = module_percents(
        {"M1": Fraction(3), "M2": Fraction(1)},
        {"M1": Fraction(4), "M2": Fraction(2), "M3": Fraction(5), "M4": Fraction(0)},
    )
    assert result == {"M1": Fraction(75), "M2": Fraction(50), "M3": Fraction(0)}
