import json
import shutil
from pathlib import Path
from typing import Any

from app.scripts.lint_content import main as lint_main
from app.trainer.lint import lint
from app.trainer.pack import load_pack

BASE = Path(__file__).resolve().parent.parent / "content/trainer"


def make_pack(tmp: Path, activities: list[dict[str, Any]]) -> Path:
    root = tmp / "t/en/A1"
    shutil.copytree(BASE / "en/A1", root)
    # Start from no practice content, so each test controls what exists.
    for shipped in (root / "activities").glob("*.json"):
        shipped.unlink()
    for n, activity in enumerate(activities):
        (root / "activities" / f"a{n}.json").write_text(
            json.dumps(activity), encoding="utf-8"
        )
    return root


def opt(i: str, text: str) -> dict[str, str]:
    return {"id": i, "text": text}


def good_activity(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "act-1",
        "lesson_id": "1.1",
        "skill": "grammar",
        "title": "To be",
        "estimated_minutes": 5,
        "items": [
            {
                "id": "i1",
                "type": "mcq",
                "prompt": "He ___ a cook in Quibdo.",
                "options": [opt("a", "am"), opt("b", "is")],
                "correct": "b",
                "explanation": "With he we use is.",
            }
        ],
    }
    return {**base, **over}


def problems(root: Path) -> str:
    return "\n".join(lint(load_pack(root)).errors)


def test_the_committed_pack_has_no_errors() -> None:
    report = lint(load_pack(BASE / "en/A1"))
    assert report.errors == []


def test_the_cli_exits_zero_on_the_committed_pack() -> None:
    assert lint_main([str(BASE)]) == 0


def test_the_cli_exits_one_on_a_broken_pack(tmp_path: Path) -> None:
    bad = good_activity(items=[])
    root = make_pack(tmp_path, [bad])
    assert lint_main([str(root.parent.parent)]) == 1


def test_a_good_activity_is_clean(tmp_path: Path) -> None:
    assert problems(make_pack(tmp_path, [good_activity()])) == ""


def test_missing_explanation_unknown_lesson_and_todo(tmp_path: Path) -> None:
    activity = good_activity(lesson_id="9.9", title="TODO fix")
    activity["items"][0]["explanation"] = " "
    text = problems(make_pack(tmp_path, [activity]))
    assert "unknown lesson 9.9" in text
    assert "without an explanation" in text
    assert "unfinished text" in text


def test_a_practice_item_that_repeats_an_exam_item_is_refused(tmp_path: Path) -> None:
    activity = good_activity()
    activity["items"][0]["prompt"] = "How much is the soup?"
    assert "repeats an exam item" in problems(make_pack(tmp_path, [activity]))


def test_the_repeat_check_normalizes_case_and_punctuation(tmp_path: Path) -> None:
    activity = good_activity()
    activity["items"][0]["prompt"] = "  what TIME does the tour leave "
    assert "repeats an exam item" in problems(make_pack(tmp_path, [activity]))


def test_options_that_collide_after_normalizing(tmp_path: Path) -> None:
    activity = good_activity()
    activity["items"][0]["options"] = [opt("a", "Is."), opt("b", "is")]
    assert "collide" in problems(make_pack(tmp_path, [activity]))


def test_gap_template_and_gaps_must_agree(tmp_path: Path) -> None:
    activity = good_activity(
        items=[
            {
                "id": "g",
                "type": "gap_fill",
                "prompt": "Fill the gaps.",
                "template": "I {1} Ana and {2} a guide.",
                "explanation": "x",
                "gaps": [{"gap_id": "1", "accepted": ["am"]}],
            }
        ]
    )
    assert "placeholders and gaps differ" in problems(make_pack(tmp_path, [activity]))


def test_a_listening_activity_needs_audio_in_the_manifest(tmp_path: Path) -> None:
    activity = good_activity(skill="listening")
    activity["items"][0]["type"] = "listen_choose"
    activity["items"][0]["audio"] = "nope"
    text = problems(make_pack(tmp_path, [activity]))
    assert "not in the manifest" in text


def test_dialogue_graph_dead_ends_and_orphans(tmp_path: Path) -> None:
    dialogue = {
        "start": "n1",
        "nodes": [
            {
                "id": "n1",
                "partner_line": "Hello, welcome to the hotel.",
                "mode": "choose",
                "options": [
                    {"id": "a", "text": "Hello.", "next": "n2"},
                    {"id": "b", "text": "Hi.", "next": "n2"},
                ],
            },
            {
                "id": "n2",
                "partner_line": "What is your name please?",
                "mode": "choose",
                "options": [
                    {"id": "a", "text": "I am Ana.", "next": "n1"},
                    {"id": "b", "text": "Ana.", "next": "n1"},
                ],
            },
            {
                "id": "n3",
                "partner_line": "Nobody gets here at all.",
                "mode": "choose",
                "options": [
                    {"id": "a", "text": "Ok.", "next": None},
                    {"id": "b", "text": "Fine.", "next": None},
                ],
            },
        ],
    }
    activity = good_activity(skill="dialogue", items=[], dialogue=dialogue)
    text = problems(make_pack(tmp_path, [activity]))
    assert "dead end" in text
    assert "unreachable dialogue node" in text


def test_published_without_a_reviewer_is_refused(tmp_path: Path) -> None:
    activity = good_activity(status="published")
    assert "published without reviewed_by" in problems(make_pack(tmp_path, [activity]))


def test_coverage_lists_lessons_with_missing_skills(tmp_path: Path) -> None:
    root = make_pack(tmp_path, [good_activity()])
    report = lint(load_pack(root))
    assert report.coverage["1.1"] == {"grammar": 1}
    assert any("lesson 1.2: no activity" in w for w in report.warnings)


def test_the_spanish_word_todo_is_not_an_unfinished_marker(tmp_path: Path) -> None:
    activity = good_activity(title="Eso es todo")
    assert "unfinished text" not in problems(make_pack(tmp_path, [activity]))
