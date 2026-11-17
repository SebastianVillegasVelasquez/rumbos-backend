"""What a student sees of a practice step: the definition minus every answer.

The definitions in a pack hold the keys, so nothing here returns one. Option,
token and pair order is shuffled by the session seed (stable on reload, different
between sessions); authored ids are kept because practice shows the right answer
after each try anyway.
"""

import hashlib
from collections.abc import Sequence
from typing import Any

from app.trainer.grading import Feedback
from app.trainer.items import (
    ChoiceItem,
    DialogueNode,
    GapFillItem,
    GradedItem,
    MatchItem,
    ReorderItem,
    ShortAnswerItem,
)


def shuffled[T](seq: Sequence[T], seed: str, key: str, ident: Any) -> list[T]:
    def order(pair: tuple[int, T]) -> str:
        i, _ = pair
        return hashlib.sha256(f"{seed}|{key}|{i}|{ident(pair[1])}".encode()).hexdigest()

    return [v for _, v in sorted(enumerate(seq), key=order)]


def _opts(options: Sequence[Any]) -> list[dict[str, str]]:
    return [{"id": o.id, "text": o.text} for o in options]


def item_view(item: GradedItem, seed: str, audio: Any) -> dict[str, Any]:
    """`audio(audio_id)` turns a manifest id into `{url, duration_s}`."""
    base: dict[str, Any] = {
        "step_id": item.id,
        "type": item.type,
        "prompt": item.prompt,
        "hint": item.hint,
        "audio": audio(item.audio) if item.audio else None,
    }
    if isinstance(item, ChoiceItem):
        base["options"] = _opts(shuffled(item.options, seed, item.id, lambda o: o.id))
    elif isinstance(item, GapFillItem):
        base["template"] = item.template
        base["gaps"] = [
            {
                "gap_id": g.gap_id,
                "options": _opts(shuffled(g.options, seed, g.gap_id, lambda o: o.id))
                if g.options
                else None,
            }
            for g in item.gaps
        ]
    elif isinstance(item, ReorderItem):
        base["tokens"] = _opts(shuffled(item.tokens, seed, item.id, lambda o: o.id))
    elif isinstance(item, MatchItem):
        base["left"] = _opts(item.left)
        base["right"] = _opts(shuffled(item.right, seed, item.id, lambda o: o.id))
    elif isinstance(item, ShortAnswerItem):
        base["max_length"] = item.max_length
    return base


def node_view(node: DialogueNode, audio: Any) -> dict[str, Any]:
    """A dialogue turn. `next` of the options is left out: where a choice leads
    is the script's business."""
    return {
        "step_id": node.id,
        "type": "dialogue_turn",
        "partner_line": node.partner_line,
        "translation": node.translation,
        "audio": audio(node.audio) if node.audio else None,
        "mode": node.mode,
        "options": [{"id": o.id, "text": o.text} for o in node.options],
        "starters": node.starters,
        "word_bank": node.word_bank,
    }


def feedback_view(fb: Feedback) -> dict[str, Any]:
    return {
        "explanation": fb.explanation,
        "closest_answer": fb.closest_answer,
        "error_tags": [t.text for t in fb.error_tags],
    }
