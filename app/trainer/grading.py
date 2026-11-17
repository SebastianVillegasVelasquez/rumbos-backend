"""Deterministic grading of practice exercises. Pure: no I/O, no clock, no
randomness, no AI. `grade` compares a response with an item definition;
`advance` steps a scripted dialogue.

A response that cannot be a response to the item (an unknown option id, a
repeated token, the wrong shape) raises `InvalidResponseError`; a wrong answer
is a normal result, never an error. Text is compared as normalized tokens, so
no student input is ever used as a pattern.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.trainer.items import (
    ChoiceItem,
    Dialogue,
    ErrorTag,
    GapFillItem,
    GradedItem,
    MatchItem,
    ReorderItem,
    ShortAnswerItem,
)
from app.trainer.language import MAX_FREE_TEXT_CHARS, LanguageRules

Outcome = Literal["correct", "partial", "incorrect", "accepted"]


class InvalidResponseError(ValueError):
    """The response is not a possible answer to this item."""


@dataclass(frozen=True)
class Feedback:
    explanation: str | None = None
    closest_answer: str | None = None
    error_tags: tuple[ErrorTag, ...] = ()


@dataclass(frozen=True)
class GradeResult:
    outcome: Outcome
    feedback: Feedback
    # Units earned and available: one per gap, per pair, per item.
    earned: int
    total: int


@dataclass(frozen=True)
class TurnResult:
    outcome: Outcome
    feedback: Feedback
    # The node to show next; None when the conversation is over or, after an
    # outcome of "incorrect", when the same node stays.
    next_node_id: str | None
    finished: bool


def _text(response: Mapping[str, Any], key: str, limit: int) -> str:
    value = response.get(key)
    if not isinstance(value, str):
        raise InvalidResponseError(f"{key} must be text")
    if len(value) > limit:
        raise InvalidResponseError(f"{key} is too long")
    return value[:MAX_FREE_TEXT_CHARS]


def _result(
    item: GradedItem,
    earned: int,
    total: int,
    closest: str | None,
    tags: tuple[ErrorTag, ...] = (),
) -> GradeResult:
    outcome: Outcome = (
        "correct" if earned == total else "partial" if earned else "incorrect"
    )
    shown = () if outcome == "correct" else tags
    return GradeResult(
        outcome,
        Feedback(
            explanation=item.explanation,
            closest_answer=None if outcome == "correct" else closest,
            error_tags=shown,
        ),
        earned,
        total,
    )


def grade(
    item: GradedItem, response: Mapping[str, Any], rules: LanguageRules
) -> GradeResult:
    if isinstance(item, ChoiceItem):
        return _grade_choice(item, response)
    if isinstance(item, GapFillItem):
        return _grade_gaps(item, response, rules)
    if isinstance(item, ReorderItem):
        return _grade_reorder(item, response, rules)
    if isinstance(item, MatchItem):
        return _grade_match(item, response)
    return _grade_short(item, response, rules)


def _grade_choice(item: ChoiceItem, response: Mapping[str, Any]) -> GradeResult:
    chosen = response.get("option_id")
    texts = {o.id: o.text for o in item.options}
    if not isinstance(chosen, str) or chosen not in texts:
        raise InvalidResponseError("unknown option")
    tag = item.error_tags.get(chosen)
    return _result(
        item,
        int(chosen == item.correct),
        1,
        texts[item.correct],
        (tag,) if tag else (),
    )


def _grade_gaps(
    item: GapFillItem, response: Mapping[str, Any], rules: LanguageRules
) -> GradeResult:
    values = response.get("values")
    if not isinstance(values, Mapping):
        raise InvalidResponseError("values must be an object")
    gaps = {g.gap_id: g for g in item.gaps}
    if not set(values) <= set(gaps):
        raise InvalidResponseError("unknown gap")
    earned = 0
    tags: list[ErrorTag] = []
    closest: list[str] = []
    for gap in item.gaps:
        value = values.get(gap.gap_id)
        if gap.options is not None:
            texts = {o.id: o.text for o in gap.options}
            if value is not None and (not isinstance(value, str) or value not in texts):
                raise InvalidResponseError("unknown option")
            ok = value is not None and value == gap.correct
            closest.append(texts.get(gap.correct or "", ""))
        else:
            if value is not None and not isinstance(value, str):
                raise InvalidResponseError("a typed gap needs text")
            typed = rules.normalize(value) if value else ""
            ok = bool(typed) and typed in {rules.normalize(a) for a in gap.accepted}
            closest.append(gap.accepted[0] if gap.accepted else "")
        if ok:
            earned += 1
        elif gap.error_tag:
            tags.append(gap.error_tag)
    return _result(item, earned, len(item.gaps), " / ".join(closest), tuple(tags))


def _grade_reorder(
    item: ReorderItem, response: Mapping[str, Any], rules: LanguageRules
) -> GradeResult:
    ids = response.get("token_ids")
    texts = {t.id: t.text for t in item.tokens}
    if (
        not isinstance(ids, list)
        or not all(isinstance(i, str) for i in ids)
        or len(ids) != len(texts)
        or set(ids) != set(texts)
    ):
        raise InvalidResponseError("token_ids must use every token once")

    def words(sequence: list[str]) -> list[str]:
        return [rules.normalize(texts[i]) for i in sequence]

    right = [words([t.id for t in item.tokens])]
    right += [words(a) for a in item.alternatives]
    ok = words(ids) in right
    closest = " ".join(t.text for t in item.tokens)
    return _result(
        item, int(ok), 1, closest, (item.error_tag,) if item.error_tag else ()
    )


def _grade_match(item: MatchItem, response: Mapping[str, Any]) -> GradeResult:
    pairs = response.get("pairs")
    left = {o.id for o in item.left}
    right = {o.id for o in item.right}
    if not isinstance(pairs, list):
        raise InvalidResponseError("pairs must be a list")
    seen_left: set[str] = set()
    seen_right: set[str] = set()
    chosen: set[tuple[str, str]] = set()
    for pair in pairs:
        if (
            not isinstance(pair, list | tuple)
            or len(pair) != 2
            or not all(isinstance(x, str) for x in pair)
        ):
            raise InvalidResponseError("a pair has two ids")
        a, b = pair
        if a not in left or b not in right:
            raise InvalidResponseError("unknown id")
        if a in seen_left or b in seen_right:
            raise InvalidResponseError("an id is used twice")
        seen_left.add(a)
        seen_right.add(b)
        chosen.add((a, b))
    earned = len(chosen & set(item.pairs))
    return _result(
        item, earned, len(item.pairs), None, (item.error_tag,) if item.error_tag else ()
    )


def _grade_short(
    item: ShortAnswerItem, response: Mapping[str, Any], rules: LanguageRules
) -> GradeResult:
    text = _text(response, "text", item.max_length)
    typed = rules.tokenize(text)
    ok = bool(typed) and any(typed == rules.tokenize(a) for a in item.accepted)
    return _result(
        item, int(ok), 1, item.accepted[0], (item.error_tag,) if item.error_tag else ()
    )


def advance(
    dialogue: Dialogue,
    node_id: str,
    response: Mapping[str, Any],
    rules: LanguageRules,
) -> TurnResult:
    """One student turn. A chosen option, or a typed reply that has every word
    of an accepted set, is `accepted` and moves on; a typed reply that matches
    nothing is `incorrect`, stays on the node and shows the model reply."""
    node = dialogue.node(node_id)
    if node is None:
        raise InvalidResponseError("unknown dialogue node")
    if node.mode == "choose":
        chosen = response.get("option_id")
        option = next((o for o in node.options if o.id == chosen), None)
        if option is None:
            raise InvalidResponseError("unknown option")
        return TurnResult("accepted", Feedback(), option.next, option.next is None)
    typed = set(rules.tokenize(_text(response, "text", MAX_FREE_TEXT_CHARS)))
    for rule in node.accept:
        if {rules.normalize(w) for w in rule.all_of} <= typed:
            return TurnResult("accepted", Feedback(), rule.next, rule.next is None)
    return TurnResult(
        "incorrect", Feedback(closest_answer=node.model_reply), None, False
    )
