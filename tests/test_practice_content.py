"""The shipped practice content answers to its own keys: every item is solved
by its key, and every dialogue can be finished with its model replies."""

from pathlib import Path
from typing import Any

import pytest

from app.trainer.grading import advance, grade
from app.trainer.items import (
    ChoiceItem,
    GapFillItem,
    GradedItem,
    MatchItem,
    ReorderItem,
    ShortAnswerItem,
)
from app.trainer.language import ENGLISH
from app.trainer.pack import Pack, load_pack

PACK: Pack = load_pack(Path("content/trainer/en/A1"))
ACTIVITIES = list(PACK.activities.values())


def solution(item: GradedItem) -> dict[str, Any]:
    if isinstance(item, ChoiceItem):
        return {"option_id": item.correct}
    if isinstance(item, GapFillItem):
        return {
            "values": {
                g.gap_id: g.correct if g.options else g.accepted[0] for g in item.gaps
            }
        }
    if isinstance(item, ReorderItem):
        return {"token_ids": [t.id for t in item.tokens]}
    if isinstance(item, MatchItem):
        return {"pairs": [list(p) for p in item.pairs]}
    assert isinstance(item, ShortAnswerItem)
    return {"text": item.accepted[0]}


def test_the_pack_covers_every_lesson_with_three_kinds_of_activity() -> None:
    by_lesson: dict[str, set[str]] = {}
    for a in ACTIVITIES:
        by_lesson.setdefault(a.lesson_id, set()).add(a.skill)
    assert set(by_lesson) == {lesson.id for lesson in PACK.curriculum.lessons}
    assert all(s == {"dialogue", "listening", "grammar"} for s in by_lesson.values())
    per_lesson: dict[str, int] = {}
    for v in PACK.vocabulary.values():
        per_lesson[v.lesson_id] = per_lesson.get(v.lesson_id, 0) + 1
    assert set(per_lesson.values()) == {8}


def test_nothing_is_published_without_a_reviewer() -> None:
    assert all(
        a.status == "draft" or (a.reviewed_by and a.reviewed_at) for a in ACTIVITIES
    )


@pytest.mark.parametrize(
    "activity", [a for a in ACTIVITIES if a.items], ids=lambda a: a.id
)
def test_every_item_is_solved_by_its_key(activity: Any) -> None:
    for item in activity.items:
        result = grade(item, solution(item), ENGLISH)
        assert result.outcome == "correct", item.id


@pytest.mark.parametrize(
    "activity", [a for a in ACTIVITIES if a.items], ids=lambda a: a.id
)
def test_a_wrong_choice_is_never_correct(activity: Any) -> None:
    for item in activity.items:
        if isinstance(item, ChoiceItem):
            wrong = next(o.id for o in item.options if o.id != item.correct)
            assert grade(item, {"option_id": wrong}, ENGLISH).outcome == "incorrect"


@pytest.mark.parametrize(
    "activity", [a for a in ACTIVITIES if a.dialogue], ids=lambda a: a.id
)
def test_every_dialogue_ends_with_its_model_replies(activity: Any) -> None:
    dialogue = activity.dialogue
    node_id = dialogue.start
    for _ in range(len(dialogue.nodes) + 1):
        node = dialogue.node(node_id)
        response = (
            {"option_id": node.options[0].id}
            if node.mode == "choose"
            else {"text": node.model_reply}
        )
        turn = advance(dialogue, node_id, response, ENGLISH)
        assert turn.outcome == "accepted", node_id
        if turn.finished:
            return
        node_id = turn.next_node_id
    raise AssertionError("the dialogue never ended")


def test_a_wrong_typed_reply_does_not_move_the_dialogue() -> None:
    typed = [
        (a.dialogue, n)
        for a in ACTIVITIES
        if a.dialogue
        for n in a.dialogue.nodes
        if n.mode == "type"
    ]
    assert typed
    for dialogue, node in typed:
        assert (
            advance(dialogue, node.id, {"text": "banana banana"}, ENGLISH).outcome
            == "incorrect"
        )
