import json
from fractions import Fraction
from pathlib import Path

import pytest

from app.assessment.rubric import (
    Response,
    Rubric,
    blank_ids,
    fill_template,
    is_placeholder,
    rubric_from,
    score,
)
from app.assessment.vocabulary import course_vocabulary
from app.trainer.assessment_form import AssessmentForm, ProductiveItem
from app.trainer.language import ENGLISH

PACK = Path(__file__).resolve().parent.parent / "content/trainer/en/A1"
FORM = AssessmentForm.model_validate_json(
    (PACK / "assessments/a1-form-a.json").read_text(encoding="utf-8")
)
RUBRICS = {
    r["id"]: rubric_from(r)
    for r in json.loads((PACK / "rubrics.json").read_text(encoding="utf-8"))["rubrics"]
}
WORDS = json.loads((PACK / "course_words.json").read_text(encoding="utf-8"))["words"]
VOCAB = course_vocabulary(FORM, WORDS, ENGLISH)
NOTE_TEMPLATE = next(
    i.template
    for i in FORM.section("writing").items()
    if isinstance(i, ProductiveItem) and i.template
)


def run(rubric_id: str, values: dict[str, str], text: str | None = None) -> Fraction:
    joined = text if text is not None else " ".join(values.values())
    return score(RUBRICS[rubric_id], Response(values, joined), VOCAB, ENGLISH).points


def note(*blanks: str) -> tuple[dict[str, str], str]:
    values = dict(zip(blank_ids(NOTE_TEMPLATE), blanks, strict=True))
    return values, fill_template(NOTE_TEMPLATE, list(blanks))


GOOD_CARD = {
    "name": "Ana Torres",
    "country": "Colombia",
    "nationality": "Colombian",
    "occupation": "Guide",
    "phone_number": "3001234567",
}


def test_every_rubric_is_worth_five_points() -> None:
    assert {k: r.max_points() for k, r in RUBRICS.items()} == dict.fromkeys(RUBRICS, 5)


def test_every_productive_item_names_a_rubric_that_exists() -> None:
    for section in (FORM.section("writing"), FORM.section("speaking")):
        for item in section.items():
            assert isinstance(item, ProductiveItem)
            assert item.rubric in RUBRICS


def test_the_template_has_five_blanks() -> None:
    assert blank_ids(NOTE_TEMPLATE) == ["b1", "b2", "b3", "b4", "b5"]


# --- Writing task 1 ------------------------------------------------------------


def test_a_good_host_card_scores_full_marks() -> None:
    assert run("writing.host_card", GOOD_CARD) == 5


def test_an_empty_host_card_scores_zero() -> None:
    assert run("writing.host_card", dict.fromkeys(GOOD_CARD, "")) == 0


def test_gibberish_in_lowercase_does_not_score() -> None:
    card = dict.fromkeys(GOOD_CARD, "asdf")
    assert run("writing.host_card", card) == 0


def test_lowercase_fields_lose_the_capital_point() -> None:
    card = {**GOOD_CARD, "name": "ana torres", "country": "colombia"}
    assert run("writing.host_card", card) < 5


def test_a_short_phone_number_loses_part_of_a_point() -> None:
    assert run("writing.host_card", {**GOOD_CARD, "phone_number": "123"}) == Fraction(
        9, 2
    )


def test_the_same_value_everywhere_is_not_plausible() -> None:
    card = {**GOOD_CARD, "country": "Colombia", "nationality": "Colombia"}
    assert run("writing.host_card", card) < 5


# --- Writing task 2 ------------------------------------------------------------


def test_a_good_welcome_note_scores_full_marks() -> None:
    values, text = note("Posada Encuentro", "Ana", "guide", "7:00", "market")
    assert 15 <= len(ENGLISH.tokenize(text)) <= 25
    assert run("writing.welcome_note", values, text) == 5


def test_an_empty_welcome_note_scores_zero() -> None:
    values, text = note("", "", "", "", "")
    assert run("writing.welcome_note", values, text) == 0


def test_a_note_without_a_time_loses_the_coherence_point() -> None:
    values, text = note("Posada Encuentro", "Ana", "guide", "soon", "market")
    assert run("writing.welcome_note", values, text) < 5


def test_a_note_that_ignores_the_template_scores_low() -> None:
    text = "I like football and my dog is big and brown and I eat rice every day ok"
    assert run("writing.welcome_note", {"b1": text}, text) < 2


# --- Speaking ------------------------------------------------------------------


def test_good_written_answers_to_the_three_questions() -> None:
    values = {
        "q1": "My name is Ana.",
        "q2": "I am from Colombia.",
        "q3": "I am a guide.",
    }
    assert run("speaking.three_questions_written", values) == 5


def test_answers_without_the_model_phrases_lose_points() -> None:
    values = {"q1": "Ana", "q2": "Colombia", "q3": "guide"}
    assert run("speaking.three_questions_written", values) < 4


def test_contractions_with_curly_apostrophes_count_as_model_phrases() -> None:
    values = {"q1": "I’m Ana.", "q2": "I’m from Peru.", "q3": "I’m a cook."}
    assert run("speaking.three_questions_written", values) == 5


def test_blank_answers_score_zero() -> None:
    assert (
        run("speaking.three_questions_written", dict.fromkeys(["q1", "q2", "q3"], ""))
        == 0
    )


def test_the_written_read_aloud_needs_the_whole_note() -> None:
    _, text = note("Posada Encuentro", "Ana", "guide", "7:00", "market")
    assert run("speaking.read_aloud_written", {"q1": text}, text) == 5
    assert run("speaking.read_aloud_written", {"q1": "Hello!"}, "Hello!") < 2


# --- properties ----------------------------------------------------------------


def test_the_same_response_always_scores_the_same() -> None:
    first = run("writing.host_card", GOOD_CARD)
    assert all(run("writing.host_card", GOOD_CARD) == first for _ in range(5))


@pytest.mark.parametrize("rubric_id", sorted(RUBRICS))
def test_scores_stay_between_zero_and_five(rubric_id: str) -> None:
    for sample in ("", "x", "Hello! " * 80, "!!!", "\u0000\u0001", "A" * 5000):
        values = {
            k: sample for k in ("name", "q1", "q2", "q3", "b1", "b2", "b3", "b4", "b5")
        }
        assert 0 <= run(rubric_id, values, sample) <= 5


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", True),
        ("   ", True),
        ("...", True),
        ("xxx", True),
        ("aaaa", True),
        ("asdf", True),
        ("N/A", True),
        ("Ana", False),
        ("7:00", False),
    ],
)
def test_placeholders(value: str, expected: bool) -> None:
    assert is_placeholder(value, ENGLISH) is expected


def test_the_known_weakness_a_capitalized_nonsense_name_is_rewarded() -> None:
    """Documented, not hidden: a capitalized word in a name position counts as
    a name, so 'Asdf' scores where 'asdf' does not."""
    lower = run("writing.host_card", {**GOOD_CARD, "name": "asdf"})
    upper = run("writing.host_card", {**GOOD_CARD, "name": "Asdf"})
    assert upper > lower


def test_the_rubric_model_refuses_an_unknown_check() -> None:
    with pytest.raises(ValueError):
        Rubric.model_validate(
            {
                "id": "x",
                "criteria": [
                    {"id": "c", "points": 1, "checks": [{"check": "does_not_exist"}]}
                ],
            }
        )


def test_a_half_filled_note_is_scaled_by_what_was_filled() -> None:
    full_values, full_text = note("Posada Encuentro", "Ana", "guide", "7:00", "market")
    half_values, half_text = note("Posada Encuentro", "Ana", "", "", "")
    full = score(
        RUBRICS["writing.welcome_note"],
        Response(full_values, full_text),
        VOCAB,
        ENGLISH,
    )
    half = score(
        RUBRICS["writing.welcome_note"],
        Response(half_values, half_text),
        VOCAB,
        ENGLISH,
    )
    assert full.by_criterion["uses_template"] == 2
    # Two of five blanks filled: all six phrases are there, scaled by 2/5.
    assert half.by_criterion["uses_template"] == Fraction(4, 5)
    assert half.points < full.points


def test_a_criterion_cannot_be_all_multipliers() -> None:
    with pytest.raises(ValueError):
        Rubric.model_validate(
            {
                "id": "x",
                "criteria": [
                    {
                        "id": "c",
                        "points": 1,
                        "checks": [{"check": "not_placeholder", "multiplier": True}],
                    }
                ],
            }
        )
