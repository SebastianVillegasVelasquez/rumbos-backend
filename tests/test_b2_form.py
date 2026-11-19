"""The B2 check-in exam (Forma A): structure, keys against the client's source,
scoring (the matching item is five units; passing is strictly above the mark),
what a student is never sent, and what the proxy rubrics can and cannot tell."""

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
from tests.test_a2_form import _right_answers

ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = ROOT / "content/trainer/en/B2"
SOURCE = json.loads(
    (ROOT / "tools/sources/b2-form-a.source.json").read_text(encoding="utf-8")
)
PACK: Pack = load_pack(PACK_DIR)
FORM: AssessmentForm = PACK.forms["b2-form-a"]
RUNTIME: FormRuntime = runtime_for(PACK, "b2-form-a", ENGLISH)
WEIGHTS = {s.key: s.weight for s in FORM.sections}


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
    assert [len(p.items) for p in FORM.section("listening").parts] == [5, 5, 1]
    assert [len(p.items) for p in FORM.section("reading").parts] == [5, 5, 5]


def test_the_weights_are_the_sources_and_speaking_counts() -> None:
    assert WEIGHTS == SOURCE["weights"]
    assert WEIGHTS["speaking"] == 15


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
    assert all(re.fullmatch(r"b2-fa-[lruws]-\d\d", i) for i in ids)
    numbers = sorted(i.source_number for i in items if i.source_number)
    assert numbers == list(range(1, 42))
    assert "b2-fa-r-01" in ids and "b2-fa-u-01" in ids


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


def test_the_form_is_published_after_review_and_says_its_level() -> None:
    assert FORM.status == "published"
    assert FORM.reviewed_by and FORM.reviewed_at
    assert PACK.meta.status == "published"
    assert (FORM.language, FORM.level, FORM.purpose) == ("en", "B2", "check_in")
    assert "B2" in FORM.title and "A1" not in FORM.title and "A2" not in FORM.title
    assert not any(
        "image_pending" in json.dumps(i.model_dump())
        for s in FORM.sections
        for i in s.items()
    )


def test_speaking_is_written_equivalent_and_holds_task_two_only() -> None:
    speaking = FORM.section("speaking")
    assert speaking.delivery == "written_equivalent" and speaking.weight == 15
    (task,) = speaking.items()
    assert isinstance(task, ProductiveItem)
    assert task.item_id == "b2-fa-s-02" and task.written_equivalent_of
    assert "I am pleased to open" in task.prompt
    assert speaking.max_points() == 5
    assert FORM.section("writing").delivery is None
    assert any("Task 1" in n and "NOT in this form" in n for n in FORM.source_notes)


def test_writing_allows_a_letter_and_shows_both_prompts_of_task_two() -> None:
    one, two = FORM.section("writing").items()
    assert isinstance(one, ProductiveItem) and isinstance(two, ProductiveItem)
    for task in (one, two):
        assert (task.min_words, task.max_words, task.max_chars) == (120, 160, 1500)
        assert [q.id for q in task.questions] == ["q1"]
    # The platform has no choice of task: both prompts are one item.
    assert "(a)" in two.prompt and "(b)" in two.prompt and "OR" in two.prompt
    assert any("no choice of task" in n for n in FORM.source_notes)
    served = section_view(FORM, "writing", "seed", lambda a: {})
    sent = served["parts"][0]["items"]
    assert [(i["min_words"], i["max_words"], i["max_chars"]) for i in sent] == [
        (120, 160, 1500)
    ] * 2


def test_the_committed_form_is_what_the_importer_builds() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "import_b2_form_a", ROOT / "tools/import_b2_form_a.py"
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
        assert [v.model_dump() for v in manifest[entry["id"]].voices] == entry["voices"]


def test_spoken_text_has_no_digits_symbols_or_acronyms_and_the_log_matches() -> None:
    forms = [e for e in PACK.audio.values() if e.id.startswith("b2fa-")]
    assert len(forms) == 15
    for entry in forms:
        for text in [entry.text, *(v.text for v in entry.voices)]:
            assert not re.search(r"\d|£|%|\b[A-Z]{2,}\b", text), (entry.id, text)
    spelled = [e for e in forms if "G D S" in e.text]
    assert len(spelled) == 2 and all("GDS" in e.transcript for e in spelled)
    assert any("GDS -> G D S" in n for n in FORM.source_notes)


def test_conversations_keep_two_voices_and_single_clips_one() -> None:
    forms = [e for e in PACK.audio.values() if e.id.startswith("b2fa-")]
    two = [e for e in forms if len(e.voices) == 2]
    assert len(two) == 5  # items 1, 6, 7, 9 and 10; item 8 is one speaker
    for entry in two:
        assert {v.voice for v in entry.voices} == {"host", "guest"}
    assert all(
        len(e.voices) == 1 and e.voices[0].voice == "host"
        for e in forms
        if e not in two
    )


# --- scoring -----------------------------------------------------------------


def test_all_objective_answers_right_is_all_of_their_weight() -> None:
    scored = score_form(RUNTIME, _right_answers(FORM), ENGLISH)
    by = {s.key: s for s in scored.total.sections}
    for key in ("listening", "reading", "use_of_english"):
        assert (by[key].points, by[key].max) == (Fraction(15), 15)
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


def test_a_blank_page_scores_zero() -> None:
    assert score_form(RUNTIME, {}, ENGLISH).total.percent == 0


def _total(
    receptive: tuple[int, int, int], writing: Fraction, speaking: Fraction
) -> Fraction:
    listening, reading, use = receptive
    return total_of(
        [
            SectionScore("listening", Fraction(listening), 15, WEIGHTS["listening"]),
            SectionScore("reading", Fraction(reading), 15, WEIGHTS["reading"]),
            SectionScore(
                "use_of_english", Fraction(use), 15, WEIGHTS["use_of_english"]
            ),
            SectionScore("writing", writing, 10, WEIGHTS["writing"]),
            SectionScore("speaking", speaking, 5, WEIGHTS["speaking"]),
        ]
    ).percent


def test_passing_is_strictly_above_eighty_with_this_forms_weights() -> None:
    perfect = (15, 15, 15)
    # 70 + 1.5 * writing + 3 * speaking: writing 20/3 of 10 is exactly 80.
    exactly_eighty = _total(perfect, Fraction(20, 3), Fraction(0))
    assert exactly_eighty == 80
    assert not passes(exactly_eighty, 80, "gt")
    assert passes(exactly_eighty, 80, "gte")
    just_above = _total(perfect, Fraction(20, 3) + Fraction(1, 100), Fraction(0))
    assert just_above > 80 and passes(just_above, 80, "gt")


def test_what_production_a_student_needs_if_every_receptive_item_is_right() -> None:
    """Production is 30 of the 100 points (writing 15 in two tasks, speaking 15 in
    one), so with the receptive part perfect (70) the student needs more than 10 of
    those 30: a third of the production weight."""
    perfect = (15, 15, 15)
    needed = Fraction(10)
    # Speaking alone: more than 10/3 of 5 points (a proxy gives fifths, so 4 of 5).
    assert not passes(_total(perfect, Fraction(0), Fraction(10, 3)), 80, "gt")
    assert passes(_total(perfect, Fraction(0), Fraction(4)), 80, "gt")
    # Writing alone: more than 20/3 of 10 points.
    assert not passes(_total(perfect, Fraction(20, 3), Fraction(0)), 80, "gt")
    assert passes(_total(perfect, Fraction(7), Fraction(0)), 80, "gt")
    # A mixed answer: one point of Speaking is worth two of Writing.
    assert passes(_total(perfect, Fraction(5), Fraction(1)), 80, "gt")
    assert needed == Fraction(1, 3) * 30


def test_what_receptive_score_a_student_needs_if_production_is_perfect() -> None:
    """With production at its maximum (30) the receptive part must be worth more
    than 50 of its 70 points. Listening and Reading items are worth 5/3 points,
    Use of English items 4/3: 30 Listening and Reading items are exactly 50, which
    is not enough, so one Use of English item more passes, and 31 of 45 is the
    least that can."""
    assert not passes(_total((15, 15, 0), Fraction(10), Fraction(5)), 80, "gt")
    assert passes(_total((15, 15, 1), Fraction(10), Fraction(5)), 80, "gt")
    # 31 of 45 correct (30 + 1) passes; 30 of 45 never does, wherever they are.
    assert not passes(_total((15, 15, 0), Fraction(10), Fraction(5)), 80, "gt")
    assert not passes(_total((10, 10, 10), Fraction(10), Fraction(5)), 80, "gt")


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
    ids: list[str] = []
    for item in FORM.section("listening").items():
        if isinstance(item, ChoiceAssessmentItem) and item.audio:
            ids.append(item.audio.audio)
        elif isinstance(item, MatchAudioItem):
            ids += [c.audio for c in item.clips]
    assert len(ids) == 15 and len(set(ids)) == 15
    for audio_id in ids:
        assert re.fullmatch(r"b2fa-[0-9a-f]{10}", audio_id)
        assert PACK.audio[audio_id].file == f"audio/{audio_id}.mp3"
        assert "b2-fa" not in PACK.audio[audio_id].file


def test_no_audio_id_is_shared_with_another_pack() -> None:
    for level in ("A1", "A2"):
        other = load_pack(ROOT / f"content/trainer/en/{level}")
        assert not set(other.audio) & set(PACK.audio)


def test_the_pack_lints_without_errors() -> None:
    report = lint(PACK)
    assert report.ok, report.errors


# --- the proxy rubrics: what they reward and what they cannot tell -------------


def _points(item_id: str, text: str) -> Fraction:
    items = {i.item_id: i for s in FORM.sections for i in s.items()}
    from app.assessment.engine import _productive_points

    item = items[item_id]
    assert isinstance(item, ProductiveItem)
    return _productive_points(RUNTIME, item, {"values": {"q1": text}}, ENGLISH)


LETTER = (
    "Dear Sir or Madam, I am writing to request an urgent review of our "
    "cooperative's account, which has been blocked since Monday. Since then, the "
    "operator has been cancelling our confirmed bookings without any warning, and "
    "we have not received a single explanation from your team. This situation has "
    "seriously affected our guests and our reputation. Therefore, we require that "
    "the account be reactivated within forty-eight hours and that every cancelled "
    "booking be restored. We also ask for a written explanation of the compliance "
    "review that you have apparently opened. Furthermore, we would be grateful if "
    "the cancellation fees could be waived, as the problem was not caused by us. "
    "Please confirm in writing what you will do and by when. We look forward to "
    "your prompt reply. Yours faithfully, Ana Torres"
)
MINUTES = (
    "The session was opened at ten, and I am pleased to say that every delegate was "
    "present. The chair asked each party for a summary of its reasons. Firstly, the "
    "cooperative argued that the fee is unfair. Secondly, the operator replied that "
    "the rules are strict. We strongly disagree, said the cooperative, and the "
    "disagreement was recorded in the minutes. However, both sides agreed to meet "
    "again. If we had not set a time limit, the session would have lasted all night. "
    "Therefore, the session was closed on time at noon. In conclusion, the positions "
    "are still far apart, but the dialogue has been opened and will continue next "
    "week with a shorter agenda and fewer speakers."
)
DEFENCE = (
    "Colombia, a Mediator's Perspective. If we had not integrated every community "
    "into the dialogue, our peace would have been much weaker. I am convinced that "
    "tourism has been a bridge between old enemies. The rivers have been protected, "
    "and the forests would be closed to loggers if the law were applied. "
    "Furthermore, villages that were once divided now share the same visitors, "
    "because they decided to cooperate. Therefore, we must keep investing in this "
    "culture of peace. If we had given up in the hard years, we would have lost the "
    "best chance in our history. In conclusion, we urge everyone to protect what has "
    "been built, and to tell the story with pride."
)
CHAIR = (
    "I am pleased to open this meeting. Could you provide a summary of your reasons? "
    "I understand your concern, but we strongly disagree with the proposed figure. "
    "If we had not protected this destination, it would have lost its value. Time "
    "is up, so I now close the meeting. Thank you all."
)
LIST = (
    "Dear. I am writing to request. We require that. Could you. Yours faithfully. "
    "Has been. Has been blocked. Compliance. Cancellation fees. Waived. However. "
) * 5


SPEAKING_LIST = (
    "I am pleased to open. Could you provide a summary of your reasons. We "
    "strongly disagree. Respect. If we had not, would have. Destination. I now "
    "close the meeting. Time is up. Thank you."
)


def test_a_real_letter_and_both_options_of_task_two_score_well() -> None:
    assert _points("b2-fa-w-01", LETTER) == 5
    assert _points("b2-fa-w-02", MINUTES) >= 4  # option (a)
    assert _points("b2-fa-w-02", DEFENCE) == 5  # option (b)
    assert _points("b2-fa-s-02", CHAIR) == 5


def test_empty_short_and_off_topic_answers_score_nothing_or_little() -> None:
    for item in ("b2-fa-w-01", "b2-fa-w-02", "b2-fa-s-02"):
        assert _points(item, "") == 0
    assert (
        _points("b2-fa-w-01", "Dear Sir, please waive the fees. Yours faithfully") == 0
    )
    assert _points("b2-fa-s-02", "I open the meeting.") == 0
    assert _points("b2-fa-w-01", "banana " * 130) == 0


def test_a_text_cut_at_the_old_cap_would_have_scored_zero() -> None:
    # Without a limit the old cap of 300 characters applies (45 words or so).
    from app.assessment.rubric import Response, score

    item = next(i for i in FORM.section("writing").items())
    assert isinstance(item, ProductiveItem)
    old = score(
        RUNTIME.rubrics[item.rubric],
        Response({"q1": LETTER}, LETTER),
        RUNTIME.vocabulary,
        ENGLISH,
    )
    assert old.points == 0 and len(LETTER) > 300


def test_known_limit_a_list_of_the_expected_phrases_scores_like_a_real_letter() -> None:
    """The proxy checks that phrases, tense markers and linking words are present
    and that the length is in range. It cannot tell a letter from a list of them,
    so Writing Task 1 gives full marks to nonsense that has the right words. This
    test pins that weakness on purpose: if it ever stops being true the REVIEW
    file is out of date."""
    assert 100 <= len(LIST.split()) <= 190
    assert _points("b2-fa-w-01", LIST) == 5
    assert _points("b2-fa-w-02", LIST) < 5
    assert _points("b2-fa-s-02", SPEAKING_LIST) == 5


@pytest.mark.parametrize(
    "rubric_id",
    [
        "writing.complaint_letter",
        "writing.one_of_two",
        "speaking.chair_negotiation_written",
    ],
)
def test_every_b2_rubric_is_worth_five_points(rubric_id: str) -> None:
    assert RUNTIME.rubrics[rubric_id].max_points() == 5
