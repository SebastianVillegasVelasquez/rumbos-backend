from typing import Any

import pytest

from app.trainer.grading import InvalidResponseError, advance, grade
from app.trainer.items import (
    ChoiceItem,
    Dialogue,
    DialogueAccept,
    DialogueChoice,
    DialogueNode,
    ErrorTag,
    Gap,
    GapFillItem,
    GradedItem,
    MatchItem,
    Option,
    ReorderItem,
    ShortAnswerItem,
)
from app.trainer.language import ENGLISH

TAG = ErrorTag(tag="agreement", text="Use is with she.")


def opts(*texts: str) -> list[Option]:
    return [Option(id=f"o{i}", text=t) for i, t in enumerate(texts)]


MCQ = ChoiceItem(
    id="c1",
    type="mcq",
    prompt="She ___ from Brazil.",
    options=opts("am", "is", "are"),
    correct="o1",
    explanation="With she we use is.",
    error_tags={"o0": TAG},
)
GAPS = GapFillItem(
    id="g1",
    type="gap_fill",
    prompt="Complete.",
    template="My name {1} Ana and I {2} from Peru.",
    explanation="Use is and am.",
    gaps=[
        Gap(gap_id="1", options=opts("is", "are"), correct="o0"),
        Gap(gap_id="2", accepted=["am"], error_tag=TAG),
    ],
)
REORDER = ReorderItem(
    id="r1",
    type="reorder",
    prompt="Order the words.",
    explanation="Subject, verb, place.",
    tokens=[
        Option(id="t1", text="I"),
        Option(id="t2", text="am"),
        Option(id="t3", text="a"),
        Option(id="t4", text="guide"),
    ],
    alternatives=[["t1", "t2", "t4"]],
)
MATCH = MatchItem(
    id="m1",
    type="match",
    prompt="Match.",
    explanation="Places and what happens there.",
    left=[Option(id="l1", text="dock"), Option(id="l2", text="pharmacy")],
    right=[Option(id="r1", text="boat"), Option(id="r2", text="medicine")],
    pairs=[("l1", "r1"), ("l2", "r2")],
)
SHORT = ShortAnswerItem(
    id="s1",
    type="short_answer",
    prompt="Say it.",
    explanation="Seven fifteen.",
    accepted=["It's 7:15", "It is 7:15"],
    max_length=40,
)


def run(item: GradedItem, response: dict[str, Any]) -> Any:
    return grade(item, response, ENGLISH)


# --- choice ------------------------------------------------------------------


def test_choice_correct() -> None:
    result = run(MCQ, {"option_id": "o1"})
    assert (result.outcome, result.earned, result.total) == ("correct", 1, 1)
    assert result.feedback.explanation == "With she we use is."
    assert result.feedback.closest_answer is None
    assert result.feedback.error_tags == ()


def test_choice_wrong_shows_tag_and_closest() -> None:
    result = run(MCQ, {"option_id": "o0"})
    assert result.outcome == "incorrect"
    assert result.feedback.closest_answer == "is"
    assert result.feedback.error_tags == (TAG,)


@pytest.mark.parametrize(
    "response", [{}, {"option_id": "zz"}, {"option_id": 1}, {"option_id": None}]
)
def test_choice_invalid(response: dict[str, Any]) -> None:
    with pytest.raises(InvalidResponseError):
        run(MCQ, response)


# --- gap fill ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "outcome", "earned"),
    [
        ({"1": "o0", "2": "am"}, "correct", 2),
        ({"1": "o0", "2": "  AM. "}, "correct", 2),
        ({"1": "o0", "2": "is"}, "partial", 1),
        ({"1": "o1", "2": "is"}, "incorrect", 0),
        ({}, "incorrect", 0),
        ({"2": ""}, "incorrect", 0),
    ],
)
def test_gaps(values: dict[str, str], outcome: str, earned: int) -> None:
    result = run(GAPS, {"values": values})
    assert (result.outcome, result.earned, result.total) == (outcome, earned, 2)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"values": []},
        {"values": {"9": "x"}},
        {"values": {"1": "nope"}},
        {"values": {"2": 5}},
    ],
)
def test_gaps_invalid(response: dict[str, Any]) -> None:
    with pytest.raises(InvalidResponseError):
        run(GAPS, response)


# --- reorder -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("ids", "outcome"),
    [
        (["t1", "t2", "t3", "t4"], "correct"),
        (["t1", "t2", "t4"], None),  # missing a token: invalid, see below
        (["t4", "t3", "t2", "t1"], "incorrect"),
    ],
)
def test_reorder(ids: list[str], outcome: str | None) -> None:
    if outcome is None:
        with pytest.raises(InvalidResponseError):
            run(REORDER, {"token_ids": ids})
    else:
        assert run(REORDER, {"token_ids": ids}).outcome == outcome


@pytest.mark.parametrize(
    "ids",
    [
        ["t1", "t1", "t2", "t3"],
        ["t1", "t2", "t3", "zz"],
        "t1t2t3t4",
        [1, 2, 3, 4],
        [],
    ],
)
def test_reorder_invalid(ids: Any) -> None:
    with pytest.raises(InvalidResponseError):
        run(REORDER, {"token_ids": ids})


# --- match -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pairs", "outcome", "earned"),
    [
        ([["l1", "r1"], ["l2", "r2"]], "correct", 2),
        ([["l1", "r1"], ["l2", "r1"]], None, 0),  # right id twice: invalid
        ([["l1", "r2"], ["l2", "r1"]], "incorrect", 0),
        ([["l1", "r1"]], "partial", 1),
        ([], "incorrect", 0),
    ],
)
def test_match(pairs: list[list[str]], outcome: str | None, earned: int) -> None:
    if outcome is None:
        with pytest.raises(InvalidResponseError):
            run(MATCH, {"pairs": pairs})
        return
    result = run(MATCH, {"pairs": pairs})
    assert (result.outcome, result.earned, result.total) == (outcome, earned, 2)


@pytest.mark.parametrize(
    "pairs",
    [
        "l1r1",
        [["l1"]],
        [["l1", "r1", "x"]],
        [["l1", 5]],
        [["zz", "r1"]],
        [["l1", "zz"]],
        [["l1", "r1"], ["l1", "r2"]],
    ],
)
def test_match_invalid(pairs: Any) -> None:
    with pytest.raises(InvalidResponseError):
        run(MATCH, {"pairs": pairs})


# --- short answer ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("It's 7:15", "correct"),
        ("it’s 7:15.", "correct"),
        ("  IT IS   7:15!  ", "correct"),
        ("It is 7:50", "incorrect"),
        ("", "incorrect"),
        ("   ", "incorrect"),
        ("7:15", "incorrect"),
    ],
)
def test_short_answer(text: str, outcome: str) -> None:
    result = run(SHORT, {"text": text})
    assert result.outcome == outcome
    if outcome == "incorrect":
        assert result.feedback.closest_answer == "It's 7:15"


@pytest.mark.parametrize("response", [{}, {"text": 3}, {"text": "x" * 41}])
def test_short_answer_invalid(response: dict[str, Any]) -> None:
    with pytest.raises(InvalidResponseError):
        run(SHORT, response)


def test_text_beyond_the_cap_is_never_processed() -> None:
    long = ShortAnswerItem(
        id="s2",
        type="short_answer",
        prompt="Say it.",
        explanation="x",
        accepted=["yes"],
        max_length=300,
    )
    assert run(long, {"text": "yes " + "x" * 290}).outcome == "incorrect"
    with pytest.raises(InvalidResponseError):
        run(long, {"text": "x" * 301})


def test_hostile_text_does_not_hang() -> None:
    result = run(SHORT, {"text": "(a+)+$" * 6})
    assert result.outcome == "incorrect"


# --- dialogue ----------------------------------------------------------------

DIALOGUE = Dialogue(
    start="n1",
    nodes=[
        DialogueNode(
            id="n1",
            partner_line="Hello! What is your name?",
            mode="choose",
            options=[
                DialogueChoice(id="a", text="My name is Ana.", next="n2"),
                DialogueChoice(id="b", text="I am Ana.", next="n2"),
            ],
        ),
        DialogueNode(
            id="n2",
            partner_line="Where are you from?",
            mode="type",
            accept=[DialogueAccept(all_of=["from", "peru"])],
            model_reply="I am from Peru.",
        ),
    ],
)


def test_dialogue_choice_advances() -> None:
    turn = advance(DIALOGUE, "n1", {"option_id": "a"}, ENGLISH)
    assert (turn.outcome, turn.next_node_id, turn.finished) == ("accepted", "n2", False)


def test_dialogue_typed_accepted_and_finished() -> None:
    turn = advance(DIALOGUE, "n2", {"text": "I'm FROM Peru."}, ENGLISH)
    assert (turn.outcome, turn.next_node_id, turn.finished) == ("accepted", None, True)


def test_dialogue_typed_rejected_stays_and_shows_model() -> None:
    turn = advance(DIALOGUE, "n2", {"text": "banana"}, ENGLISH)
    assert turn.outcome == "incorrect" and not turn.finished
    assert turn.feedback.closest_answer == "I am from Peru."


@pytest.mark.parametrize(
    ("node", "response"),
    [("zz", {"option_id": "a"}), ("n1", {"option_id": "zz"}), ("n2", {"text": 4})],
)
def test_dialogue_invalid(node: str, response: dict[str, Any]) -> None:
    with pytest.raises(InvalidResponseError):
        advance(DIALOGUE, node, response, ENGLISH)
