"""A written task may allow a longer answer than the shared cap of 300
characters; without the setting nothing changes."""

import pytest
from pydantic import ValidationError

from app.assessment.engine import _item_view, clean_response
from app.assessment.rubric import Response, Rubric, rubric_from, score
from app.trainer.assessment_form import ProductiveItem
from app.trainer.language import ENGLISH, MAX_FREE_TEXT_CHARS

LETTER = " ".join(["Dear Sir or Madam, we have been waiting for an answer."] * 13)


def _item(**over: object) -> ProductiveItem:
    return ProductiveItem.model_validate(
        {
            "item_id": "x-w-01",
            "type": "written_answers",
            "prompt": "Write a letter.",
            "rubric": "r",
            "questions": [{"id": "q1", "text": "Your letter"}],
            "source_rubric": "source",
            **over,
        }
    )


def _words_rubric() -> Rubric:
    return rubric_from(
        {
            "id": "r",
            "criteria": [
                {
                    "id": "length",
                    "points": 5,
                    "checks": [
                        {
                            "check": "word_count_between",
                            "on": ["q1"],
                            "min": 100,
                            "max": 180,
                        }
                    ],
                }
            ],
        }
    )


def test_the_letter_used_here_is_longer_than_the_shared_cap() -> None:
    assert len(LETTER) > 2 * MAX_FREE_TEXT_CHARS
    assert 120 <= len(LETTER.split()) <= 160


def test_without_max_chars_an_answer_is_still_cut_at_300() -> None:
    kept = clean_response("f", _item(), {"values": {"q1": LETTER}})
    assert len(kept["values"]["q1"]) == MAX_FREE_TEXT_CHARS


def test_with_max_chars_the_longer_answer_is_kept_up_to_the_limit() -> None:
    kept = clean_response("f", _item(max_chars=1200), {"values": {"q1": LETTER}})
    assert kept["values"]["q1"] == LETTER[:1200]
    assert len(kept["values"]["q1"]) == min(len(LETTER), 1200)


def test_the_rubric_counts_the_words_of_a_long_answer_only_when_allowed() -> None:
    rubric = _words_rubric()
    response = Response({"q1": LETTER}, LETTER)
    short = score(rubric, response, frozenset(), ENGLISH)
    long = score(rubric, response, frozenset(), ENGLISH, 1200)
    assert short.points == 0  # only the first ~45 words were seen
    assert long.points == 5


def test_max_chars_is_not_for_a_template() -> None:
    with pytest.raises(ValidationError):
        _item(type="template_text", template="a ______ b", max_chars=500)
    with pytest.raises(ValidationError):
        _item(max_chars=0)


def test_the_served_item_carries_the_length_hints_only_when_the_form_sets_them() -> (
    None
):
    def audio(_: str) -> dict[str, object]:
        return {}

    plain = _item_view("f", _item(), "seed", audio)
    assert not {"min_words", "max_words", "max_chars"} & set(plain)
    sized = _item_view(
        "f",
        _item(min_words=120, max_words=160, max_chars=1200),
        "seed",
        audio,
    )
    assert (sized["min_words"], sized["max_words"], sized["max_chars"]) == (
        120,
        160,
        1200,
    )
