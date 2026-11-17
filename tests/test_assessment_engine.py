import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.assessment.engine import (
    FormRuntime,
    clean_response,
    option_id,
    runtime_for,
    score_form,
    section_view,
)
from app.assessment.scoring import passes
from app.trainer.assessment_form import (
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
)
from app.trainer.grading import InvalidResponseError
from app.trainer.language import ENGLISH
from app.trainer.pack import load_pack

PACK = load_pack(Path(__file__).resolve().parent.parent / "content/trainer/en/A1")
RT: FormRuntime = runtime_for(PACK, "a1-form-a", ENGLISH)
FORM = RT.form
FID = FORM.form_id
SECTIONS = [s.key for s in FORM.sections]


def audio(audio_id: str) -> dict[str, Any]:
    return {"url": f"/audio/{audio_id}", "duration_s": 3.0}


def view(seed: str, key: str) -> dict[str, Any]:
    return section_view(FORM, key, seed, audio)


GOOD_TEXT: dict[str, dict[str, str]] = {
    "writing.host_card": {
        "name": "Ana Torres",
        "country": "Colombia",
        "nationality": "Colombian",
        "occupation": "Guide",
        "phone_number": "3001234567",
    },
    "writing.welcome_note": {
        "b1": "Posada Encuentro",
        "b2": "Ana",
        "b3": "guide",
        "b4": "7:00",
        "b5": "market",
    },
    "speaking.three_questions_written": {
        "q1": "My name is Ana.",
        "q2": "I am from Colombia.",
        "q3": "I am a guide.",
    },
}
NOTE = (
    "Hello! Welcome to Posada Encuentro. My name is Ana and I am a guide. "
    "Breakfast is at 7:00. You can visit market. Enjoy your stay!"
)


def answers(kind: str) -> dict[str, dict[str, Any]]:
    """`right` or `wrong` objective answers, with good or empty writing."""
    out: dict[str, dict[str, Any]] = {}
    for item_id, item in RT.items().items():
        if isinstance(item, ChoiceAssessmentItem):
            wrong = next(o.id for o in item.options if o.id != item.key)
            letter = item.key if kind == "right" else wrong
            out[item_id] = {"option_id": option_id(FID, item_id, letter)}
        elif isinstance(item, MatchAudioItem):
            tokens = view("x", "listening")  # clip ids are seed independent
            clip_ids = {
                c["audio"]["url"]: c["id"]
                for part in tokens["parts"]
                for it in part["items"]
                if it["type"] == "match_audio"
                for c in it["clips"]
            }
            places = [p.id for p in item.places]
            pairs = []
            for index, place in item.key:
                clip = clip_ids[f"/audio/{item.clips[index].audio}"]
                chosen = (
                    place if kind == "right" else next(p for p in places if p != place)
                )
                pairs.append([clip, chosen])
            out[item_id] = {"pairs": pairs}
        elif isinstance(item, ProductiveItem) and kind == "right":
            if item.rubric == "speaking.read_aloud_written":
                out[item_id] = {"values": {"q1": NOTE}}
            else:
                out[item_id] = {"values": GOOD_TEXT[item.rubric]}
    return out


# --- scoring end to end ------------------------------------------------------------


def test_everything_right_is_100_and_passes() -> None:
    scored = score_form(RT, answers("right"), ENGLISH)
    assert scored.total.percent == 100
    assert passes(scored.total.percent, 80, "gt")
    assert scored.total.band == "achieved"
    assert {s.key: s.points for s in scored.total.sections}["listening"] == 15
    assert all(p == 100 for p in scored.modules.values())


def test_everything_wrong_is_zero() -> None:
    scored = score_form(RT, answers("wrong"), ENGLISH)
    assert scored.total.percent == 0
    assert scored.total.band == "emerging"
    assert not passes(scored.total.percent, 80, "gte")


def test_nothing_answered_is_zero() -> None:
    assert score_form(RT, {}, ENGLISH).total.percent == 0


def test_objective_only_is_70_and_does_not_pass() -> None:
    right = {k: v for k, v in answers("right").items() if "values" not in v}
    scored = score_form(RT, right, ENGLISH)
    assert scored.total.percent == 70
    assert scored.total.band == "consolidating"
    assert not passes(scored.total.percent, 80, "gte")


def test_the_five_matching_pairs_count_five_points() -> None:
    wrong = answers("right")
    match_id = "listening.c.01"
    first = wrong[match_id]["pairs"][0]
    other = next(p.id for p in RT.items()[match_id].places if p.id != first[1])  # type: ignore[union-attr]
    wrong[match_id] = {"pairs": [[first[0], other], *wrong[match_id]["pairs"][1:]]}
    scored = score_form(RT, wrong, ENGLISH)
    assert scored.item_points[match_id] == 4
    assert {s.key: s.points for s in scored.total.sections}["listening"] == 14


def test_the_module_map_covers_single_module_objective_items() -> None:
    scored = score_form(RT, answers("right"), ENGLISH)
    assert set(scored.modules) == {"M1", "M2", "M3", "M4"}


def test_a_gibberish_exam_does_not_pass() -> None:
    gib = answers("right")
    for item_id, item in RT.items().items():
        if isinstance(item, ProductiveItem):
            gib[item_id] = {
                "values": {
                    k: "asdf"
                    for k in (
                        "b1",
                        "b2",
                        "b3",
                        "b4",
                        "b5",
                        "q1",
                        "q2",
                        "q3",
                        "name",
                        "country",
                        "nationality",
                        "occupation",
                        "phone_number",
                    )
                }
            }
    scored = score_form(RT, gib, ENGLISH)
    assert scored.total.percent < 80


# --- what the student sees ---------------------------------------------------------


def all_views(seed: str) -> list[dict[str, Any]]:
    return [view(seed, key) for key in SECTIONS]


def test_no_key_leaves_the_server() -> None:
    text = json.dumps(all_views("seed-1"), ensure_ascii=False)
    forbidden = {"key", "correct", "transcript", "rubric", "accepted", "competence"}
    for sec in all_views("seed-1"):
        for part in sec["parts"]:
            for item in part["items"]:
                assert not forbidden & set(item), item["item_id"]
                for option in item.get("options", []):
                    assert set(option) == {"option_id", "text"}
    assert "✓" not in text
    # Nothing marks a right answer, and no listening text is served.
    for item in RT.items().values():
        if isinstance(item, ChoiceAssessmentItem) and item.audio:
            assert item.audio.transcript not in text


def test_option_ids_do_not_reveal_the_source_letters() -> None:
    ids = {
        o["option_id"]
        for views in all_views("seed-1")
        for part in views["parts"]
        for item in part["items"]
        for o in item.get("options", [])
    }
    assert ids and not ids & {"a", "b", "c", "d", "e"}
    assert all(len(i) == 9 for i in ids)


def test_the_shuffle_is_stable_per_seed_and_differs_between_seeds() -> None:
    assert view("s1", "reading") == view("s1", "reading")
    assert view("s1", "reading") != view("s2", "reading")


def test_the_correct_option_is_not_always_first() -> None:
    first_is_right = 0
    total = 0
    for seed in ("a", "b", "c", "d", "e", "f"):
        for part in view(seed, "reading")["parts"]:
            for item in part["items"]:
                real = RT.items()[item["item_id"]]
                assert isinstance(real, ChoiceAssessmentItem)
                total += 1
                first_is_right += item["options"][0]["option_id"] == option_id(
                    FID, real.item_id, real.key
                )
    assert 0 < first_is_right < total * 0.6


def test_items_stay_inside_their_part_and_nothing_is_lost() -> None:
    for section in FORM.sections:
        served = view("s1", section.key)
        for source, shown in zip(section.parts, served["parts"], strict=True):
            assert {i.item_id for i in source.items} == {
                i["item_id"] for i in shown["items"]
            }
            for item in shown["items"]:
                real = RT.items()[item["item_id"]]
                if isinstance(real, ChoiceAssessmentItem):
                    assert len(item["options"]) == len(real.options)


def test_productive_tasks_keep_their_place_after_the_objective_items() -> None:
    for key in ("writing", "speaking"):
        types = [i["type"] for p in view("s1", key)["parts"] for i in p["items"]]
        assert types[0] in ("form_fields", "written_answers")


def test_listening_audio_is_served_without_a_transcript() -> None:
    part = view("s1", "listening")["parts"][0]
    assert all("transcript" not in i["audio"] for i in part["items"])


# --- responses ---------------------------------------------------------------------


def choice() -> ChoiceAssessmentItem:
    item = RT.items()["reading.a.01"]
    assert isinstance(item, ChoiceAssessmentItem)
    return item


def test_a_choice_response_must_be_a_served_option() -> None:
    item = choice()
    served = option_id(FID, item.item_id, item.options[0].id)
    assert clean_response(FID, item, {"option_id": served}) == {"option_id": served}
    for bad in ({}, {"option_id": "a"}, {"option_id": 3}, {"option_id": None}):
        with pytest.raises(InvalidResponseError):
            clean_response(FID, item, bad)


def test_text_values_are_capped_and_ids_checked() -> None:
    item = RT.items()["writing.tasks.01"]
    ok = clean_response(FID, item, {"values": {"name": "x" * 1000}})
    assert len(ok["values"]["name"]) == 300
    bads: list[dict[str, Any]] = [
        {"values": {"hacker": "x"}},
        {"values": {"name": 5}},
        {"values": []},
    ]
    for bad in bads:
        with pytest.raises(InvalidResponseError):
            clean_response(FID, item, bad)


def test_a_match_response_refuses_unknown_and_repeated_ids() -> None:
    item = RT.items()["listening.c.01"]
    clip = next(
        c["id"]
        for p in view("s", "listening")["parts"]
        for i in p["items"]
        if i["type"] == "match_audio"
        for c in i["clips"]
    )
    place = item.places[0].id  # type: ignore[union-attr]
    assert clean_response(FID, item, {"pairs": [[clip, place]]}) == {
        "pairs": [[clip, place]]
    }
    bad: dict[str, Any]
    for bad in (
        {"pairs": [["zz", place]]},
        {"pairs": [[clip, "zz"]]},
        {"pairs": [[clip, place], [clip, place]]},
        {"pairs": "x"},
        {"pairs": [[clip]]},
    ):
        with pytest.raises(InvalidResponseError):
            clean_response(FID, item, bad)


def test_exact_fractions_are_used_throughout() -> None:
    scored = score_form(RT, answers("right"), ENGLISH)
    assert isinstance(scored.total.percent, Fraction)
