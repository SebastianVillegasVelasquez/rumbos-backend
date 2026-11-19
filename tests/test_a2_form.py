"""The A2 check-in exam (Forma A): structure, keys against the client's source,
scoring (the matching item is five units; passing is strictly above the mark)
and what a student is never sent."""

import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.assessment.engine import FormRuntime, runtime_for, score_form, section_view
from app.assessment.scoring import SectionScore, passes, total_of
from app.trainer.assessment_form import (
    AssessmentForm,
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
)
from app.trainer.language import ENGLISH
from app.trainer.lint import lint
from app.trainer.pack import Pack, load_pack

ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = ROOT / "content/trainer/en/A2"
SOURCE = json.loads(
    (ROOT / "tools/sources/a2-form-a.source.json").read_text(encoding="utf-8")
)
PACK: Pack = load_pack(PACK_DIR)
FORM: AssessmentForm = PACK.forms["a2-form-a"]
RUNTIME: FormRuntime = runtime_for(PACK, "a2-form-a", ENGLISH)


def _objective(section: str) -> int:
    total = 0
    for item in FORM.section(section).items():
        if isinstance(item, ChoiceAssessmentItem):
            total += 1
        elif isinstance(item, MatchAudioItem):
            total += item.points
    return total


def test_sections_weights_and_unit_counts() -> None:
    assert [(s.key, s.weight) for s in FORM.sections] == [
        ("listening", 25),
        ("reading", 25),
        ("use_of_english", 20),
        ("writing", 15),
        ("speaking", 15),
    ]
    assert sum(s.weight for s in FORM.sections) == 100
    assert [_objective(k) for k in ("listening", "reading", "use_of_english")] == [
        15,
        15,
        15,
    ]
    assert sum(_objective(k) for k in ("listening", "reading", "use_of_english")) == 45


def test_the_matching_item_is_five_units() -> None:
    (match,) = [
        i for i in FORM.section("listening").items() if isinstance(i, MatchAudioItem)
    ]
    assert match.points == 5 and len(match.clips) == 5 and len(match.places) == 5
    assert sorted(i for i, _ in match.key) == [0, 1, 2, 3, 4]
    assert len({place for _, place in match.key}) == 5


def test_ids_are_stable_unique_and_independent_of_the_source_numbering() -> None:
    items = [i for s in FORM.sections for i in s.items()]
    ids = [i.item_id for i in items]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"a2-fa-[lruws]-\d\d", i) for i in ids)
    numbers = sorted(i.source_number for i in items if i.source_number)
    assert numbers == list(range(1, 42))
    # The source numbers matching as 11 and continues at 12: our ids restart
    # their own count in each section.
    assert "a2-fa-r-01" in ids and "a2-fa-u-01" in ids


def test_exactly_one_key_per_item_and_it_is_one_of_the_options() -> None:
    for section in FORM.sections:
        for item in section.items():
            if isinstance(item, ChoiceAssessmentItem):
                assert item.key in {o.id for o in item.options}, item.item_id
                assert len({o.id for o in item.options}) == len(item.options)


def test_keys_are_exactly_the_sources() -> None:
    by_number = {
        i.source_number: i for s in FORM.sections for i in s.items() if i.source_number
    }
    checked = 0
    for raw in SOURCE["items"]:
        item = by_number[raw["source_number"]]
        if raw.get("type") == "matching":
            assert isinstance(item, MatchAudioItem)
            scenes = {p.id: p.text for p in item.places}
            got = {clip + 1: scenes[place] for clip, place in item.key}
            assert got == {
                k["audio"]: k["scene"] for k in SOURCE["matching_item_11"]["key"]
            }
            continue
        assert isinstance(item, ChoiceAssessmentItem)
        right = next(o.text for o in item.options if o.id == item.key)
        wanted = next(o["text"] for o in raw["options"] if o["label"] == raw["key"])
        assert right == wanted, raw["source_number"]
        assert [o.text for o in item.options] == [o["text"] for o in raw["options"]]
        assert item.prompt == raw["stem"]
        checked += 1
    assert checked == 40


def test_every_objective_item_has_a_competence_tag() -> None:
    lessons = {lesson.id for lesson in PACK.curriculum.lessons}
    for section in FORM.sections:
        for item in section.items():
            if isinstance(item, ProductiveItem):
                continue
            assert item.competence is not None, item.item_id
            assert item.competence.modules, item.item_id
            if item.competence.lesson:
                assert ".".join(item.competence.lesson.split(".")[:2]) in lessons


def test_the_form_is_a_draft_nobody_has_reviewed() -> None:
    assert FORM.status == "published"
    assert FORM.reviewed_by and FORM.reviewed_at
    assert PACK.meta.status == "published"
    assert FORM.language == "en" and FORM.level == "A2"


def test_speaking_is_written_equivalent_and_holds_task_two_only() -> None:
    speaking = FORM.section("speaking")
    assert speaking.delivery == "written_equivalent" and speaking.weight == 15
    (task,) = speaking.items()
    assert isinstance(task, ProductiveItem)
    assert task.item_id == "a2-fa-s-02" and task.written_equivalent_of
    assert [q.text for q in task.questions] == [
        "What are you going to do tomorrow?",
        "What is the technician doing now?",
    ]
    assert speaking.max_points() == 5
    assert FORM.section("writing").delivery is None
    assert any("Task 1" in n and "NOT in this form" in n for n in FORM.source_notes)


def test_the_committed_form_is_what_the_importer_builds() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "import_a2_form_a", ROOT / "tools/import_a2_form_a.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built, entries = module.build(SOURCE)
    # Publishing only changes the review fields, never the content.
    review = {"status", "reviewed_by", "reviewed_at"}
    assert built.model_dump(exclude=review) == FORM.model_dump(exclude=review)
    manifest = {e.id: e for e in PACK.audio.values()}
    for entry in entries:
        assert manifest[entry["id"]].transcript == entry["transcript"]
        assert manifest[entry["id"]].text == entry["text"]


# --- scoring -----------------------------------------------------------------


def _right_answers(form: AssessmentForm) -> dict[str, dict[str, Any]]:
    from app.assessment.engine import _token, option_id

    answers: dict[str, dict[str, Any]] = {}
    for section in form.sections:
        for item in section.items():
            if isinstance(item, ChoiceAssessmentItem):
                answers[item.item_id] = {
                    "option_id": option_id(form.form_id, item.item_id, item.key)
                }
            elif isinstance(item, MatchAudioItem):
                answers[item.item_id] = {
                    "pairs": [
                        [
                            "c" + _token(form.form_id, item.item_id, str(i)),
                            place,
                        ]
                        for i, place in item.key
                    ]
                }
    return answers


def test_all_objective_answers_right_is_all_of_their_weight() -> None:
    scored = score_form(RUNTIME, _right_answers(FORM), ENGLISH)
    by = {s.key: s for s in scored.total.sections}
    assert (by["listening"].points, by["listening"].max) == (Fraction(15), 15)
    assert (by["reading"].points, by["reading"].max) == (Fraction(15), 15)
    assert (by["use_of_english"].points, by["use_of_english"].max) == (Fraction(15), 15)
    assert by["writing"].points == 0 and by["speaking"].points == 0
    assert scored.total.percent == Fraction(70)


def test_three_of_five_pairs_score_three_units() -> None:
    answers = _right_answers(FORM)
    (match,) = [
        i for i in FORM.section("listening").items() if isinstance(i, MatchAudioItem)
    ]
    answers[match.item_id]["pairs"] = answers[match.item_id]["pairs"][:3]
    scored = score_form(RUNTIME, answers, ENGLISH)
    assert scored.item_points[match.item_id] == 3
    assert scored.total.percent == Fraction(70) - 2 * Fraction(25, 15)


def test_a_wrong_option_scores_nothing_and_a_blank_page_scores_zero() -> None:
    assert score_form(RUNTIME, {}, ENGLISH).total.percent == 0


def test_passing_is_strictly_above_eighty_with_this_forms_weights() -> None:
    weights = {s.key: s.weight for s in FORM.sections}

    def total(writing: Fraction) -> Fraction:
        return total_of(
            [
                SectionScore("listening", Fraction(15), 15, weights["listening"]),
                SectionScore("reading", Fraction(15), 15, weights["reading"]),
                SectionScore(
                    "use_of_english", Fraction(15), 15, weights["use_of_english"]
                ),
                SectionScore("writing", writing, 10, weights["writing"]),
                SectionScore("speaking", Fraction(0), 5, weights["speaking"]),
            ]
        ).percent

    exactly_eighty = total(Fraction(20, 3))  # 70 + (20/3 / 10) * 15 = 80
    assert exactly_eighty == 80
    assert not passes(exactly_eighty, 80, "gt")
    assert passes(exactly_eighty, 80, "gte")
    just_above = total(Fraction(20, 3) + Fraction(1, 100))
    assert just_above > 80 and passes(just_above, 80, "gt")


# --- what a student is sent ----------------------------------------------------


def _audio(audio_id: str) -> dict[str, Any]:
    return {"url": f"/api/v1/trainer/audio/{audio_id}", "duration_s": None}


def test_nothing_that_reveals_an_answer_is_served() -> None:
    views = [section_view(FORM, s.key, "seed", _audio) for s in FORM.sections]
    served = json.dumps(views)
    for banned in ("transcript", "source_number", "delivery", "competence"):
        assert banned not in served
    option_ids = re.findall(r'"option_id": "([^"]+)"', served)
    assert option_ids and all(re.fullmatch(r"o[0-9a-f]{8}", o) for o in option_ids)
    for section in FORM.sections:
        for item in section.items():
            if isinstance(item, ChoiceAssessmentItem) and item.audio:
                assert item.audio.transcript not in served
            if isinstance(item, MatchAudioItem):
                assert all(c.transcript not in served for c in item.clips)
            if isinstance(item, ProductiveItem):
                assert item.source_rubric not in served
    assert "rubric" not in served


def test_audio_ids_and_files_say_nothing_about_the_item() -> None:
    listening_items = [
        i
        for i in FORM.section("listening").items()
        if not isinstance(i, ProductiveItem)
    ]
    ids: list[str] = []
    for item in listening_items:
        if isinstance(item, ChoiceAssessmentItem) and item.audio:
            ids.append(item.audio.audio)
        elif isinstance(item, MatchAudioItem):
            ids += [c.audio for c in item.clips]
    assert len(ids) == 15 and len(set(ids)) == 15
    for audio_id in ids:
        assert re.fullmatch(r"a2fa-[0-9a-f]{10}", audio_id)
        assert PACK.audio[audio_id].file == f"audio/{audio_id}.mp3"
        assert "a2-fa" not in PACK.audio[audio_id].file


def test_no_audio_id_is_shared_with_another_pack() -> None:
    a1 = load_pack(ROOT / "content/trainer/en/A1")
    assert not set(a1.audio) & set(PACK.audio)


def test_the_pack_lints_without_errors() -> None:
    report = lint(PACK)
    assert report.ok, report.errors


# --- the proxy rubrics -----------------------------------------------------------


def _points(item_id: str, values: dict[str, str]) -> Fraction:
    items = {i.item_id: i for s in FORM.sections for i in s.items()}
    from app.assessment.engine import _productive_points

    item = items[item_id]
    assert isinstance(item, ProductiveItem)
    return _productive_points(RUNTIME, item, {"values": values}, ENGLISH)


EMAIL = (
    "Dear Liam, I'm writing to confirm your reservation. I'd like to confirm a "
    "double room from June 5 for two nights. Breakfast is at 7:00. The Wi-Fi "
    "password is on your key card. Thank you."
)
STORY = {
    "b1": "walked to the river and visited a farm",
    "b2": "ate lunch and played with a dog",
    "b3": "went back to the hotel and rested",
}


def test_the_confirmation_email_rewards_the_data_and_the_formulas() -> None:
    assert _points("a2-fa-w-01", {"q1": EMAIL}) >= 4
    assert _points("a2-fa-w-01", {"q1": ""}) == 0
    assert _points("a2-fa-w-01", {"q1": "room nights breakfast wifi"}) < 2


def test_the_past_story_needs_past_verbs_not_just_the_template() -> None:
    assert _points("a2-fa-w-02", STORY) >= 4
    assert _points("a2-fa-w-02", {"b1": "", "b2": "", "b3": ""}) == 0
    present = {
        "b1": "walk to the river",
        "b2": "eat lunch there",
        "b3": "go back home now",
    }
    assert _points("a2-fa-w-02", present) < _points("a2-fa-w-02", STORY)


def test_the_speaking_answers_check_going_to_and_the_present_continuous() -> None:
    good = {"q1": "I am going to visit a coffee farm", "q2": "He is fixing the heater"}
    assert _points("a2-fa-s-02", good) == 5
    assert _points("a2-fa-s-02", {"q1": "", "q2": ""}) == 0
    no_forms = {"q1": "I visit a coffee farm", "q2": "He fixes the heater"}
    assert _points("a2-fa-s-02", no_forms) < _points("a2-fa-s-02", good)


@pytest.mark.parametrize(
    "rubric_id",
    [
        "writing.confirmation_email",
        "writing.past_story",
        "speaking.tomorrow_now_written",
    ],
)
def test_every_a2_rubric_is_worth_five_points(rubric_id: str) -> None:
    assert RUNTIME.rubrics[rubric_id].max_points() == 5
