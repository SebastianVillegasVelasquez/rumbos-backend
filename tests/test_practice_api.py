# ruff: noqa: F811
"""The practice trainer through the real stack, on a small synthetic pack."""

import dataclasses
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.main import app
from app.trainer.catalog import Catalog
from app.trainer.dependencies import get_catalog
from app.trainer.pack import Activity, VocabEntry, load_pack
from tests.test_assessment_api import (  # noqa: F401
    COURSE,
    FakeClock,
    clock,
    draft_content_is_served,
    env,
)

REAL = load_pack(Path("content/trainer/en/A1"))

EXERCISE = {
    "id": "t.ex",
    "lesson_id": "1.1",
    "skill": "grammar",
    "title": "Test exercise",
    "estimated_minutes": 5,
    "items": [
        {
            "id": "t.mcq",
            "type": "mcq",
            "prompt": "Pick the right one",
            "options": [
                {"id": "a", "text": "is"},
                {"id": "b", "text": "are"},
                {"id": "c", "text": "am"},
            ],
            "correct": "b",
            "error_tags": {"a": {"tag": "be_agreement", "text": "Use are with you."}},
            "explanation": "You takes are.",
        },
        {
            "id": "t.gap",
            "type": "gap_fill",
            "prompt": "Fill",
            "template": "I {g1} a guide.",
            "gaps": [{"gap_id": "g1", "accepted": ["am"]}],
            "explanation": "I takes am.",
        },
        {
            "id": "t.reorder",
            "type": "reorder",
            "prompt": "Order",
            "tokens": [
                {"id": "t1", "text": "Where"},
                {"id": "t2", "text": "are"},
                {"id": "t3", "text": "you"},
            ],
            "explanation": "Question order.",
        },
        {
            "id": "t.match",
            "type": "match",
            "prompt": "Match",
            "left": [{"id": "l1", "text": "Hello"}, {"id": "l2", "text": "Bye"}],
            "right": [{"id": "r1", "text": "Hi"}, {"id": "r2", "text": "Goodbye"}],
            "pairs": [["l1", "r1"], ["l2", "r2"]],
            "explanation": "Greetings.",
        },
        {
            "id": "t.short",
            "type": "short_answer",
            "prompt": "Say your country",
            "accepted": ["I am from Colombia"],
            "explanation": "Be from.",
        },
    ],
}
DIALOGUE = {
    "id": "t.dlg",
    "lesson_id": "1.1",
    "skill": "dialogue",
    "title": "Test dialogue",
    "estimated_minutes": 3,
    "dialogue": {
        "start": "n1",
        "nodes": [
            {
                "id": "n1",
                "partner_line": "Hello!",
                "mode": "choose",
                "options": [
                    {"id": "o1", "text": "Hi", "next": "n2"},
                    {"id": "o2", "text": "Hello", "next": "n2"},
                ],
            },
            {
                "id": "n2",
                "partner_line": "What is your name?",
                "mode": "type",
                "accept": [{"all_of": ["my", "name"], "next": None}],
                "model_reply": "My name is Ana.",
            },
        ],
    },
}
WORDS = [
    VocabEntry(id="v.1", lesson_id="1.1", word="hello", translation="hola"),
    VocabEntry(id="v.2", lesson_id="1.1", word="bye", translation="adios"),
]


@pytest.fixture
def small_pack() -> Iterator[None]:
    acts = [Activity.model_validate(a) for a in (EXERCISE, DIALOGUE)]
    pack = dataclasses.replace(
        REAL,
        activities={a.id: a for a in acts},
        vocabulary={w.id: w for w in WORDS},
    )
    app.dependency_overrides[get_catalog] = lambda: Catalog({("en", "A1"): pack})
    yield
    app.dependency_overrides.pop(get_catalog, None)


ON = {"trainer": {"enabled": True}}


async def begin(s: Any, activity: str = "t.ex") -> dict[str, Any]:
    r = await s.post("/trainer/sessions", json={"activity_id": activity})
    assert r.status_code in (200, 201), r.text
    body: dict[str, Any] = r.json()
    body["_status"] = r.status_code
    return body


async def ans(
    s: Any, sid: str, step: str, response: dict[str, Any], attempt: str | None = None
) -> Any:
    return await s.post(
        f"/trainer/sessions/{sid}/answers",
        json={
            "step_id": step,
            "attempt_id": attempt or str(uuid.uuid4()),
            "response": response,
        },
    )


async def test_trainer_is_off_until_a_map_enables_it(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"]({})
    s = await env["student"]()
    off = await s.get("/trainer/catalog")
    assert off.status_code == 200
    assert off.json() == {"enabled": False, "reason": "disabled_for_map"}
    assert (
        await s.post("/trainer/sessions", json={"activity_id": "t.ex"})
    ).status_code == 404


async def test_the_catalog_lists_modules_lessons_and_state(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    cat = (await s.get("/trainer/catalog")).json()
    assert (cat["language"], cat["level"]) == ("en", "A1")
    assert [m["key"] for m in cat["modules"]] == ["M1", "M2", "M3", "M4"]
    lesson = cat["modules"][0]["lessons"][0]
    assert lesson["id"] == "1.1" and lesson["vocabulary_count"] == 2
    assert {a["id"]: a["state"] for a in lesson["activities"]} == {
        "t.ex": "not_started",
        "t.dlg": "not_started",
    }
    assert {a["id"]: a["step_count"] for a in lesson["activities"]} == {
        "t.ex": 5,
        "t.dlg": 2,
    }


async def test_a_session_view_never_carries_an_answer(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    view = await begin(s)
    assert view["_status"] == 201 and view["kind"] == "exercise"
    blob = json.dumps(view)
    for secret in ("correct", "accepted", "pairs", "explanation", "error_tags"):
        assert f'"{secret}"' not in blob, secret
    assert [x["step_id"] for x in view["steps"]] == [
        "t.mcq",
        "t.gap",
        "t.reorder",
        "t.match",
        "t.short",
    ]


async def test_starting_again_resumes_the_same_session_with_the_same_order(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    first = await begin(s)
    again = await begin(s)
    assert again["_status"] == 200 and again["session_id"] == first["session_id"]
    assert again["steps"] == first["steps"]
    other = await env["student"]("someone-else")
    assert (await begin(other))["session_id"] != first["session_id"]


async def test_graded_feedback_and_idempotent_retries(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    sid = (await begin(s))["session_id"]
    wrong = await ans(s, sid, "t.mcq", {"option_id": "a"})
    body = wrong.json()
    assert (body["outcome"], body["earned"], body["total"]) == ("incorrect", 0, 1)
    assert body["feedback"] == {
        "explanation": "You takes are.",
        "closest_answer": "are",
        "error_tags": ["Use are with you."],
    }
    # A retried request (same attempt id) is answered again, not counted twice.
    attempt = str(uuid.uuid4())
    first = await ans(s, sid, "t.gap", {"values": {"g1": "am"}}, attempt)
    second = await ans(s, sid, "t.gap", {"values": {"g1": "am"}}, attempt)
    assert first.json() == second.json() and first.json()["outcome"] == "correct"
    summary = (await s.post(f"/trainer/sessions/{sid}/finish")).json()["summary"]
    assert summary["tries"] == 2 and summary["steps_answered"] == 2


async def test_every_item_type_is_graded(env: dict[str, Any], small_pack: None) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    sid = (await begin(s))["session_id"]
    cases: list[tuple[str, dict[str, Any], str | None]] = [
        ("t.mcq", {"option_id": "b"}, "correct"),
        ("t.gap", {"values": {"g1": "am"}}, "correct"),
        ("t.reorder", {"token_ids": ["t1", "t2", "t3"]}, "correct"),
        ("t.match", {"pairs": [["l1", "r1"], ["l2", "r1"]]}, None),  # r1 twice: invalid
        ("t.match", {"pairs": [["l1", "r1"], ["l2", "r2"]]}, "correct"),
        ("t.short", {"text": "i am   from colombia!"}, "correct"),
    ]
    for step, response, outcome in cases:
        r = await ans(s, sid, step, response)
        if outcome is None:
            assert (
                r.status_code == 422
                and r.json()["detail"]["code"] == "invalid_response"
            )
        else:
            assert r.json()["outcome"] == outcome, step


async def test_the_summary_counts_first_tries_and_names_what_to_revise(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    sid = (await begin(s))["session_id"]
    await ans(s, sid, "t.mcq", {"option_id": "a"})  # wrong first
    await ans(s, sid, "t.mcq", {"option_id": "b"})  # right second: still a miss
    await ans(s, sid, "t.gap", {"values": {"g1": "am"}})
    done = (await s.post(f"/trainer/sessions/{sid}/finish")).json()["summary"]
    assert done["steps_total"] == 5 and done["first_try_correct"] == 1
    assert done["percent"] == 20 and done["to_revise"] == ["be_agreement"]
    assert done["missed_steps"] == ["t.mcq"]
    again = (await s.post(f"/trainer/sessions/{sid}/finish")).json()["summary"]
    assert again == done
    r = await ans(s, sid, "t.short", {"text": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "finished"
    cat = (await s.get("/trainer/catalog")).json()
    ex = next(
        a for a in cat["modules"][0]["lessons"][0]["activities"] if a["id"] == "t.ex"
    )
    assert (ex["state"], ex["best_percent"]) == ("finished", 20)


async def test_a_dialogue_goes_turn_by_turn(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    view = await begin(s, "t.dlg")
    sid = view["session_id"]
    assert view["kind"] == "dialogue" and [x["step_id"] for x in view["steps"]] == [
        "n1"
    ]
    assert "next" not in json.dumps(view["steps"])
    out_of_turn = await ans(s, sid, "n2", {"text": "my name"})
    assert out_of_turn.status_code == 409
    first = (await ans(s, sid, "n1", {"option_id": "o1"})).json()
    assert first["outcome"] == "accepted" and first["next"]["step_id"] == "n2"
    # A typed reply that is not accepted stays on the turn and shows a model.
    miss = (await ans(s, sid, "n2", {"text": "banana"})).json()
    assert (
        miss["outcome"] == "incorrect"
        and miss["feedback"]["closest_answer"] == "My name is Ana."
    )
    last = (await ans(s, sid, "n2", {"text": "My name is Ana"})).json()
    assert last["finished"] is True and last["next"] is None
    done = (await s.get(f"/trainer/sessions/{sid}")).json()
    assert done["status"] == "finished" and done["summary"]["steps_total"] == 2


async def test_a_retried_dialogue_turn_does_not_advance_twice(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    sid = (await begin(s, "t.dlg"))["session_id"]
    attempt = str(uuid.uuid4())
    one = await ans(s, sid, "n1", {"option_id": "o1"}, attempt)
    two = await ans(s, sid, "n1", {"option_id": "o1"}, attempt)
    assert one.json()["next"]["step_id"] == two.json()["next"]["step_id"] == "n2"
    view = (await s.get(f"/trainer/sessions/{sid}")).json()
    assert [x["step_id"] for x in view["steps"]] == ["n2"] and view["turn"] == 2


async def test_nobody_else_can_use_my_session(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    mine, other = await env["student"]("alice"), await env["student"]("bob")
    sid = (await begin(mine))["session_id"]
    assert (await other.get(f"/trainer/sessions/{sid}")).status_code == 404
    assert (await ans(other, sid, "t.mcq", {"option_id": "a"})).status_code == 404
    assert (await other.post(f"/trainer/sessions/{sid}/finish")).status_code == 404


async def test_vocabulary_moves_through_leitner_boxes(
    env: dict[str, Any], small_pack: None, clock: FakeClock
) -> None:
    await env["setup"](ON)
    s = await env["student"]()
    listing = (await s.get("/trainer/vocabulary?lesson=1.1")).json()
    assert [(e["word"], e["saved"]) for e in listing] == [
        ("hello", False),
        ("bye", False),
    ]
    saved = (await s.put("/trainer/vocabulary/v.1")).json()
    assert (saved["box"], saved["due"]) == (1, True)
    assert (await s.put("/trainer/vocabulary/v.1")).json()["box"] == 1  # idempotent
    assert len((await s.get("/trainer/vocabulary/saved?due=true")).json()) == 1
    boxes = []
    for _ in range(6):
        boxes.append(
            (
                await s.post("/trainer/vocabulary/v.1/review", json={"knew_it": True})
            ).json()["box"]
        )
    assert boxes == [2, 3, 4, 5, 5, 5]
    assert (
        await s.get("/trainer/vocabulary/saved?due=true")
    ).json() == []  # due in 16 days
    clock.advance(days=17)
    assert len((await s.get("/trainer/vocabulary/saved?due=true")).json()) == 1
    miss = (
        await s.post("/trainer/vocabulary/v.1/review", json={"knew_it": False})
    ).json()
    assert miss["box"] == 1 and miss["due"] is False  # tomorrow
    progress = (await s.get("/trainer/progress")).json()
    assert (progress["words_saved"], progress["words_by_box"]["1"]) == (1, 1)
    assert (
        await s.post("/trainer/vocabulary/v.2/review", json={"knew_it": True})
    ).status_code == 404
    assert (await s.put("/trainer/vocabulary/nope")).status_code == 404
    assert (await s.delete("/trainer/vocabulary/v.1")).status_code == 204
    assert (await s.get("/trainer/vocabulary/saved")).json() == []


async def test_reports_are_written_by_students_and_read_by_editors_only(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    s, ed = await env["student"](), await env["editor"]()
    r = await s.post(
        "/trainer/reports",
        json={"activity_id": "t.ex", "step_id": "t.mcq", "text": "Two answers fit"},
    )
    assert r.status_code == 201
    assert (await s.get("/trainer/reports")).status_code == 403
    rows = (await ed.get("/trainer/reports")).json()
    assert [(x["activity_id"], x["step_id"], x["text"]) for x in rows] == [
        ("t.ex", "t.mcq", "Two answers fit")
    ]
    assert (
        await s.post("/trainer/reports", json={"activity_id": "nope", "text": "x"})
    ).status_code == 404


async def test_erase_me_removes_only_my_practice_data(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    me, other = await env["student"]("me"), await env["student"]("other")
    await begin(me)
    await me.put("/trainer/vocabulary/v.1")
    await me.post("/trainer/reports", json={"activity_id": "t.ex", "text": "x"})
    await begin(other)
    gone = (await me.delete("/trainer/me")).json()
    assert gone == {"sessions": 1, "words": 1, "reports": 1}
    cat = (await me.get("/trainer/catalog")).json()
    assert all(
        a["state"] == "not_started"
        for a in cat["modules"][0]["lessons"][0]["activities"]
    )
    theirs = (await other.get("/trainer/catalog")).json()
    assert any(
        a["state"] == "in_progress"
        for a in theirs["modules"][0]["lessons"][0]["activities"]
    )


async def test_another_course_and_tenant_see_nothing_of_mine(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    me = await env["student"]()
    sid = (await begin(me))["session_id"]
    elsewhere = await env["student"]("x", course=COURSE + 1)
    assert (await elsewhere.get("/trainer/catalog")).json() == {
        "enabled": False,
        "reason": "disabled_for_map",
    }
    assert (await elsewhere.get(f"/trainer/sessions/{sid}")).status_code == 404


async def test_draft_activities_are_not_served_in_production(
    env: dict[str, Any], small_pack: None
) -> None:
    from app.core.config import get_settings

    await env["setup"](ON)
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": False}
    )
    s = await env["student"]()
    cat = (await s.get("/trainer/catalog")).json()
    assert cat == {"enabled": False, "reason": "no_published_content"}
    assert (
        await s.post("/trainer/sessions", json={"activity_id": "t.ex"})
    ).status_code == 404


# --- review in the real UI: editors see unpublished content, students do not -----------


def only_published_served() -> None:
    from app.core.config import get_settings

    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": False}
    )


def states(cat: dict[str, Any]) -> list[str]:
    return [
        a["publication_state"]
        for m in cat["modules"]
        for lesson in m["lessons"]
        for a in lesson["activities"]
    ]


async def test_an_editor_in_preview_sees_draft_and_reviewed_with_their_state(
    env: dict[str, Any], small_pack: None
) -> None:
    only_published_served()  # as in production
    await env["setup"](ON)
    pack = app.dependency_overrides[get_catalog]()
    pack = pack.packs[("en", "A1")]
    reviewed = pack.activities["t.dlg"].model_copy(update={"status": "reviewed"})
    app.dependency_overrides[get_catalog] = lambda: Catalog(
        {
            ("en", "A1"): dataclasses.replace(
                pack, activities={**pack.activities, "t.dlg": reviewed}
            )
        }
    )
    ed = await env["editor"]()
    cat = (await ed.get("/trainer/catalog")).json()
    assert cat["enabled"] is True
    assert sorted(states(cat)) == ["draft", "reviewed"]
    assert (
        await ed.post("/trainer/sessions", json={"activity_id": "t.ex"})
    ).status_code in (200, 201)
    # The same server, the same content: a student still gets nothing.
    s = await env["student"]()
    assert (await s.get("/trainer/catalog")).json() == {
        "enabled": False,
        "reason": "no_published_content",
    }
    assert (
        await s.post("/trainer/sessions", json={"activity_id": "t.ex"})
    ).status_code == 404


async def test_a_student_sees_published_items_flagged_published(
    env: dict[str, Any], small_pack: None
) -> None:
    only_published_served()
    await env["setup"](ON)
    pack = app.dependency_overrides[get_catalog]().packs[("en", "A1")]
    live = {
        k: a.model_copy(update={"status": "published"})
        for k, a in pack.activities.items()
        if k == "t.ex"
    }
    app.dependency_overrides[get_catalog] = lambda: Catalog(
        {("en", "A1"): dataclasses.replace(pack, activities=live)}
    )
    s = await env["student"]()
    cat = (await s.get("/trainer/catalog")).json()
    assert states(cat) == ["published"]


async def test_a_map_with_no_pack_for_its_level_says_no_published_content(
    env: dict[str, Any], small_pack: None
) -> None:
    await env["setup"](ON)
    app.dependency_overrides[get_catalog] = lambda: Catalog({})
    s = await env["student"]()
    assert (await s.get("/trainer/catalog")).json() == {
        "enabled": False,
        "reason": "no_published_content",
    }
