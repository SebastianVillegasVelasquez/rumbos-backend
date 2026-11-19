"""The B2 practice pack answers to its own keys and does not leak the exam."""

import re
from pathlib import Path
from typing import Any

import pytest

from app.trainer.assessment_form import (
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
)
from app.trainer.grading import advance, grade
from app.trainer.items import ChoiceItem, GapFillItem, MatchItem, ReorderItem
from app.trainer.language import ENGLISH
from app.trainer.pack import Pack, load_pack
from tests.test_practice_content import solution

ROOT = Path(__file__).resolve().parent.parent
PACK: Pack = load_pack(ROOT / "content/trainer/en/B2")
ACTIVITIES = list(PACK.activities.values())
FORM = PACK.forms["b2-form-a"]


def test_the_pack_covers_every_lesson_with_the_a1_shape() -> None:
    by_lesson: dict[str, set[str]] = {}
    for a in ACTIVITIES:
        by_lesson.setdefault(a.lesson_id, set()).add(a.skill)
    assert set(by_lesson) == {lesson.id for lesson in PACK.curriculum.lessons}
    assert len(by_lesson) == 18
    assert all(s == {"dialogue", "listening", "grammar"} for s in by_lesson.values())
    per_lesson: dict[str, int] = {}
    for v in PACK.vocabulary.values():
        per_lesson[v.lesson_id] = per_lesson.get(v.lesson_id, 0) + 1
    assert set(per_lesson.values()) == {8} and len(per_lesson) == 18
    shapes = {
        a.skill: a.step_count for a in ACTIVITIES
    }  # dialogue 6, listening 3, grammar 6
    assert shapes == {"dialogue": 6, "listening": 3, "grammar": 6}


def test_nothing_is_published_or_reviewed() -> None:
    assert all(a.status == "published" for a in ACTIVITIES)
    assert all(a.reviewed_by and a.reviewed_at for a in ACTIVITIES)


@pytest.mark.parametrize(
    "activity", [a for a in ACTIVITIES if a.items], ids=lambda a: a.id
)
def test_every_item_is_solved_by_its_key_and_models_the_answer(activity: Any) -> None:
    for item in activity.items:
        assert grade(item, solution(item), ENGLISH).outcome == "correct", item.id
        assert item.explanation and item.explanation.strip()
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


def test_a_nonsense_typed_reply_is_not_accepted() -> None:
    typed = [
        (a.dialogue, n)
        for a in ACTIVITIES
        if a.dialogue
        for n in a.dialogue.nodes
        if n.mode == "type"
    ]
    assert len(typed) == 18
    for dialogue, node in typed:
        result = advance(dialogue, node.id, {"text": "banana banana"}, ENGLISH)
        assert result.outcome == "incorrect"


def test_correct_options_are_not_always_in_the_same_place() -> None:
    positions = {
        item.correct
        for a in ACTIVITIES
        for item in a.items
        if isinstance(item, ChoiceItem)
    }
    assert positions == {"o1", "o2", "o3"}


# --- no leakage ------------------------------------------------------------------


def _norm(text: str) -> str:
    return ENGLISH.normalize(text)


def _exam_texts() -> dict[str, str]:
    """Normalised exam text -> where it is: stems, passages, option sets, audio."""
    found: dict[str, str] = {}
    for section in FORM.sections:
        for part in section.parts:
            if part.shared and part.shared.text:
                found[_norm(part.shared.text)] = "passage"
            for item in part.items:
                found[_norm(item.prompt)] = item.item_id
                if isinstance(item, ChoiceAssessmentItem):
                    found["|".join(sorted(_norm(o.text) for o in item.options))] = (
                        item.item_id
                    )
                    if item.audio:
                        found[_norm(item.audio.transcript)] = item.item_id
                elif isinstance(item, MatchAudioItem):
                    for clip in item.clips:
                        found[_norm(clip.transcript)] = item.item_id
                elif isinstance(item, ProductiveItem):
                    found[_norm(item.template or item.prompt)] = item.item_id
    for entry in PACK.audio.values():
        if entry.id.startswith("b2fa-"):
            found[_norm(entry.text)] = entry.id
            for voice in entry.voices:
                found[_norm(voice.text)] = entry.id
    found.pop("", None)
    return found


def _training_texts() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for a in ACTIVITIES:
        for item in a.items:
            out.append((f"{item.id} prompt", item.prompt))
            if isinstance(item, GapFillItem):
                out.append((f"{item.id} template", item.template))
            if isinstance(item, ChoiceItem):
                out.append(
                    (
                        f"{item.id} options",
                        "|".join(sorted(_norm(o.text) for o in item.options)),
                    )
                )
            for gap in item.gaps if isinstance(item, GapFillItem) else []:
                if gap.options:
                    out.append(
                        (
                            f"{item.id} gap options",
                            "|".join(sorted(_norm(o.text) for o in gap.options)),
                        )
                    )
            if isinstance(item, ReorderItem):
                out.append((f"{item.id} order", " ".join(t.text for t in item.tokens)))
            if isinstance(item, MatchItem):
                out += [(f"{item.id} left", o.text) for o in item.left]
        if a.dialogue:
            for node in a.dialogue.nodes:
                out.append((f"{a.id}/{node.id} line", node.partner_line))
                out += [(f"{a.id}/{node.id} option", o.text) for o in node.options]
    for entry in PACK.audio.values():
        if not entry.id.startswith("b2fa-"):
            out.append((f"audio {entry.id}", entry.text))
            out.append((f"audio {entry.id} transcript", entry.transcript))
    return out


def test_no_training_text_equals_an_exam_stem_passage_option_set_or_audio() -> None:
    exam = _exam_texts()
    assert len(exam) > 100
    leaks = [
        f"{where}: {text!r} = exam {exam[_norm(text)]}"
        for where, text in _training_texts()
        if _norm(text) in exam or text in exam
    ]
    assert not leaks, leaks


def test_the_leak_check_would_catch_a_copied_exam_stem() -> None:
    exam = _exam_texts()
    assert _norm("When is the delegation expected to arrive?") in exam
    assert (
        _norm(
            "It is highly probable that the keynote will be moved to the main auditorium."
        )
        in exam
    )
    assert "|".join(sorted(_norm(t) for t in ("A", "B"))) not in exam


def test_audio_ids_are_unique_across_the_pack() -> None:
    ids = [e.id for e in PACK.audio.values()]
    assert len(ids) == len(set(ids)) == 15 + 18 * 9
    used = set()
    for a in ACTIVITIES:
        for item in a.items:
            if item.audio:
                used.add(item.audio)
        if a.dialogue:
            used |= {n.audio for n in a.dialogue.nodes if n.audio}
    assert used <= set(ids) and len(used) == 18 * 9


def test_the_modules_have_the_lessons_the_exam_codes_refer_to() -> None:
    per_module: dict[str, list[str]] = {}
    for lesson in PACK.curriculum.lessons:
        per_module.setdefault(lesson.module, []).append(lesson.id)
    assert {k: len(v) for k, v in per_module.items()} == {
        "M1": 6,
        "M2": 5,
        "M3": 4,
        "M4": 3,
    }
    # Every lesson code the exam uses (1.5, 1.6, 2.4 ...) exists in the pack.
    codes = {
        ".".join(item.competence.lesson.split(".")[:2])
        for s in FORM.sections
        for item in s.items()
        if not isinstance(item, ProductiveItem)
        and item.competence
        and item.competence.lesson
    }
    assert codes <= {lesson.id for lesson in PACK.curriculum.lessons}


def test_spoken_text_is_plain_words_and_every_expansion_is_in_the_review() -> None:
    import re

    review = (ROOT / "content/trainer/en/REVIEW-B2-checkin.md").read_text(
        encoding="utf-8"
    )
    changed = []
    for entry in PACK.audio.values():
        if entry.id.startswith("b2fa-"):
            continue
        for text in [entry.text, *(v.text for v in entry.voices)]:
            assert not re.search(r"\d|£|%|\b[A-Z]{2,}\b", text), (entry.id, text)
        shown = re.sub(r"^— ", "", entry.transcript).replace(" — ", " ")
        if shown.replace("’", "'") != entry.text:
            changed.append(entry.id)
    assert changed
    for clip_id in changed:
        assert clip_id in review, f"{clip_id} is spoken differently and not logged"


def test_conversations_have_two_voices_and_dialogue_partners_one() -> None:
    two = [
        e
        for e in PACK.audio.values()
        if not e.id.startswith("b2fa-") and len(e.voices) == 2
    ]
    assert len(two) >= 20
    for entry in two:
        assert {v.voice for v in entry.voices} == {"host", "guest"}
    for entry in PACK.audio.values():
        if re.fullmatch(r"en-b2-\d\.\d\.d\d", entry.id):
            assert [v.voice for v in entry.voices] == ["guest"]
