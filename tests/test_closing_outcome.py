"""The student's journey as the frontend reads it from `GET /trainer/closing`:
the check-in outcome, the final's status and whether the SCORM condition is met.

Four states of a student (check-in passed, check-in not passed, final passed,
final exhausted) plus not taken; the gate on and off; the exemption switch; and
that nothing here is stored (it is read from the attempts)."""

import json
from typing import Any

import httpx
import pytest

from app.closing.gate import (
    Decision,
    FinalOutcome,
    Progress,
    ScormGate,
    final_outcome,
    scorm_gate,
)
from tests import test_closing_api as api
from tests.fakes import FakeCompletionProvider
from tests.test_assessment_api import COURSE, exams, put, start
from tests.test_assessment_engine import answers as engine_answers
from tests.test_publications_api import publish
from tests.test_welcome_api import put_settings

# The exam's own fixtures (a published map, students, editors, a fake clock).
env = api.env
clock = api.clock
draft_content_is_served = api.draft_content_is_served
provider = api.provider
mode = api.mode

world, learner, uid = api.world, api.learner, api.uid
closing_of, try_final, refusal = api.closing_of, api.try_final, api.refusal
SCORM_A, SCORM_B = api.SCORM_A, api.SCORM_B


async def sit(c: httpx.AsyncClient, purpose: str, answers: str) -> dict[str, Any]:
    """Starts an exam, answers it (`right` or `wrong`) and submits it."""
    attempt = (await start(c, purpose))["attempt_id"]
    await put(c, attempt, answers)
    r = await c.post(f"/trainer/attempts/{attempt}/submit")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


# --- the pure rules ---------------------------------------------------------------------

OPEN = Decision(True)


@pytest.mark.parametrize(
    ("decision", "facts", "expected"),
    [
        (OPEN, {}, FinalOutcome("required")),
        (
            Decision(False, "scorm_incomplete", Progress(1, 2)),
            {},
            FinalOutcome("locked", "scorm_incomplete"),
        ),
        (
            Decision(False, "progress_unavailable"),
            {},
            FinalOutcome("locked", "progress_unavailable"),
        ),
        (Decision(False, "user_unlinked"), {}, FinalOutcome("locked", "user_unlinked")),
        (
            Decision(False, "form_unavailable"),
            {},
            FinalOutcome("locked", "form_unavailable"),
        ),
        (Decision(False, "not_enabled"), {}, FinalOutcome("locked", "not_enabled")),
        (Decision(False, "no_final_step"), {}, FinalOutcome("locked", "no_final_step")),
        (
            Decision(False, "already_passed"),
            {"final_passed": True},
            FinalOutcome("passed"),
        ),
        (
            Decision(False, "already_passed"),
            {"checkin_exempts": True},
            FinalOutcome("not_required_passed_checkin"),
        ),
        (
            Decision(False, "no_attempts_left"),
            {"attempts_left": False},
            FinalOutcome("exhausted"),
        ),
        # A passed check-in decides it even while the final form is not published.
        (
            Decision(False, "form_unavailable"),
            {"checkin_exempts": True},
            FinalOutcome("not_required_passed_checkin"),
        ),
        # Having passed the final wins over a check-in pass.
        (
            Decision(False, "already_passed"),
            {"final_passed": True, "checkin_exempts": True},
            FinalOutcome("passed"),
        ),
    ],
)
def test_the_final_status_for_each_verdict(
    decision: Decision, facts: dict[str, Any], expected: FinalOutcome
) -> None:
    kwargs = {"final_passed": False, "checkin_exempts": False, "attempts_left": True}
    assert final_outcome(decision, **{**kwargs, **facts}) == expected


def test_a_check_in_pass_satisfies_the_scorm_condition() -> None:
    missing = Decision(False, "scorm_incomplete", Progress(0, 2))
    assert scorm_gate(missing, checkin_exempts=True) == ScormGate(
        True, "passed_checkin"
    )
    assert scorm_gate(missing, checkin_exempts=False) == ScormGate(False)
    assert scorm_gate(
        Decision(True, None, Progress(2, 2)), checkin_exempts=False
    ) == ScormGate(True)
    assert scorm_gate(
        Decision(False, "progress_unavailable"), checkin_exempts=False
    ) == ScormGate(None)
    assert scorm_gate(Decision(True), checkin_exempts=False) == ScormGate(None)


# --- nothing taken ----------------------------------------------------------------------


async def test_a_student_who_has_sat_nothing(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    outcome = (await closing_of(await learner(env, "alumno1")))["outcome"]
    assert outcome["checkin"] == {
        "status": "not_taken",
        "percent": None,
        "pass_percent": 80,
        "pass_operator": "gt",
        "attempts": {"used": 0, "max": 1},
    }
    assert outcome["final"] == {
        "status": "locked",
        "reason": "scorm_incomplete",
        "attempts": {"used": 0, "max": 3},
        **NO_SCORE,
    }
    assert outcome["scorm_gate"] == {"satisfied": False, "reason": None}


async def test_the_final_is_required_once_the_content_is_done(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, uid("alumno1"), SCORM_A, SCORM_B)
    body = await closing_of(await learner(env, "alumno1"))
    assert body["available"] is True
    assert body["outcome"]["final"]["status"] == "required"
    assert body["outcome"]["final"]["reason"] is None
    assert body["outcome"]["scorm_gate"] == {"satisfied": True, "reason": None}


# --- the check-in passed ------------------------------------------------------------------


async def test_a_passed_check_in_makes_the_final_not_required(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await learner(env, "alumno1")
    assert (await sit(s, "check_in", "right"))["passed"] is True

    body = await closing_of(s)
    outcome = body["outcome"]
    assert outcome["checkin"]["status"] == "passed"
    assert outcome["checkin"]["percent"] == "100.00"
    assert (
        outcome["checkin"]["pass_percent"],
        outcome["checkin"]["pass_operator"],
    ) == (80, "gt")
    assert outcome["checkin"]["attempts"] == {"used": 1, "max": 1}
    assert outcome["final"] == {
        "status": "not_required_passed_checkin",
        "reason": None,
        "attempts": {"used": 0, "max": 3},
        **NO_SCORE,
    }
    assert outcome["scorm_gate"] == {"satisfied": True, "reason": "passed_checkin"}
    # The verdict itself is unchanged: not available, because already passed.
    assert (body["available"], body["reason"]) == (False, "already_passed")
    # No Moodle read was needed to say so.
    assert provider.calls == 0


async def test_the_start_guard_rejects_a_final_after_a_passed_check_in(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(
        COURSE, uid("alumno1"), SCORM_A, SCORM_B
    )  # even with content done
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "right")
    assert refusal(await try_final(s))["reason"] == "already_passed"
    assert (await closing_of(s))["outcome"]["final"]["attempts"]["used"] == 0


async def test_without_the_exemption_a_passed_check_in_does_not_exempt(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, final={"exemption": {"enabled": False}})
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "right")
    outcome = (await closing_of(s))["outcome"]
    assert outcome["checkin"]["status"] == "passed"
    assert outcome["final"]["status"] == "locked"
    assert outcome["scorm_gate"]["reason"] is None


# --- the check-in not passed ----------------------------------------------------------------


async def test_a_failed_check_in_keeps_the_final_required(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await learner(env, "alumno1")
    assert (await sit(s, "check_in", "wrong"))["passed"] is False

    outcome = (await closing_of(s))["outcome"]
    assert outcome["checkin"]["status"] == "not_passed"
    assert outcome["checkin"]["percent"] is not None
    assert outcome["checkin"]["attempts"] == {"used": 1, "max": 1}
    assert outcome["final"]["status"] == "locked"
    assert outcome["final"]["reason"] == "scorm_incomplete"

    provider.complete(COURSE, uid("alumno1"), SCORM_A, SCORM_B)
    outcome = (await closing_of(s))["outcome"]
    assert outcome["final"] == {
        "status": "required",
        "reason": None,
        "attempts": {"used": 0, "max": 3},
        **NO_SCORE,
    }
    assert (await try_final(s)).status_code == 201


async def test_the_threshold_reported_is_the_one_the_attempt_was_graded_with(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "right")
    # The editor tightens the rule afterwards and publishes again.
    editor = env["client"]()
    found = (await editor.get("/course-maps", params={"moodleCourseId": COURSE})).json()
    map_id = found["items"][0]["id"]
    tighter = {
        "assessments": exams(passPercent=95, passOperator="gte"),
        "closing": api.CLOSING,
    }
    assert (await put_settings(editor, map_id, tighter)).status_code == 200
    await publish(editor, map_id)
    outcome = (await closing_of(s))["outcome"]["checkin"]
    assert (outcome["pass_percent"], outcome["pass_operator"]) == (80, "gt")
    assert outcome["status"] == "passed"


# --- the final's score and threshold -------------------------------------------------------


def rule_of(body: dict[str, Any]) -> tuple[Any, Any]:
    return body["pass_percent"], body["pass_operator"]


async def change_rule(env: dict[str, Any], **rule: Any) -> None:
    """The editor changes the pass rule and publishes again."""
    editor = env["client"]()
    found = (await editor.get("/course-maps", params={"moodleCourseId": COURSE})).json()
    map_id = found["items"][0]["id"]
    changed = {"assessments": exams(**rule), "closing": api.CLOSING}
    assert (await put_settings(editor, map_id, changed)).status_code == 200
    await publish(editor, map_id)


async def sit_part(c: httpx.AsyncClient, right: int) -> dict[str, Any]:
    """A final with only the first `right` correct answers saved."""
    attempt = (await start(c, "final"))["attempt_id"]
    items = list(engine_answers("right").items())[:right]
    payload = [{"item_id": k, "response": v} for k, v in items]
    saved = await c.put(
        f"/trainer/attempts/{attempt}/answers", json={"answers": payload}
    )
    assert saved.status_code == 200, saved.text
    body: dict[str, Any] = (await c.post(f"/trainer/attempts/{attempt}/submit")).json()
    return {**body, "attempt_id": attempt}


async def test_a_result_carries_the_rule_its_attempt_was_graded_with(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, uid("alumno1"), SCORM_A, SCORM_B)
    s = await learner(env, "alumno1")
    check_in = await sit(s, "check_in", "wrong")
    assert rule_of(check_in) == (80, "gt")
    first = await sit_part(s, 10)
    assert rule_of(first) == (80, "gt")

    await change_rule(env, passPercent=95, passOperator="gte")
    # Reloading the old result: its own rule, not the new one.
    again = (await s.get(f"/trainer/attempts/{first['attempt_id']}/result")).json()
    assert rule_of(again) == (80, "gt")
    assert rule_of(await sit_part(s, 10)) == (95, "gte")


async def test_the_final_outcome_tells_the_deciding_attempts_score_and_rule(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, uid("alumno1"), SCORM_A, SCORM_B)
    s = await learner(env, "alumno1")
    best = await sit_part(s, 15)
    assert best["passed"] is False and best["percent"] != "0.00"

    # A worse attempt under a new rule: the earlier, better one still decides,
    # with the rule it was graded with.
    await change_rule(env, passPercent=95, passOperator="gte")
    worse = await sit_part(s, 5)
    assert float(worse["percent"]) < float(best["percent"])
    final = (await closing_of(s))["outcome"]["final"]
    assert {k: final[k] for k in score("")} == score(best["percent"])
    # The latest attempt's own numbers are in its result.
    assert rule_of(worse) == (95, "gte")

    # A pass decides over any score, with its rule.
    passed = await sit(s, "final", "right")
    assert passed["passed"] is True
    final = (await closing_of(s))["outcome"]["final"]
    assert {k: final[k] for k in score("")} == score("100.00", 95, "gte")


async def test_with_no_final_sat_the_outcome_gives_the_current_rule(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, final={"passPercent": 70, "passOperator": "gte"})
    final = (await closing_of(await learner(env, "alumno1")))["outcome"]["final"]
    assert {k: final[k] for k in score("")} == {
        "percent": None,
        "pass_percent": 70,
        "pass_operator": "gte",
    }


async def test_a_map_without_a_final_exam_has_no_final_rule(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, final={"final": None}, closing=None)
    final = (await closing_of(await learner(env, "alumno1")))["outcome"]["final"]
    assert final["attempts"] is None
    assert (final["percent"], final["pass_percent"], final["pass_operator"]) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("gate", ["observe", "enforce"])
async def test_the_final_score_is_there_in_both_modes_that_report_it(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any, gate: str
) -> None:
    await world(env)
    mode(gate)
    provider.complete(COURSE, uid("alumno1"), SCORM_A, SCORM_B)
    s = await learner(env, "alumno1")
    result = await sit_part(s, 10)
    final = (await closing_of(s))["outcome"]["final"]
    assert {k: final[k] for k in score("")} == score(result["percent"])


# --- the final ----------------------------------------------------------------------------------


async def test_a_passed_final(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, uid("alumno1"), SCORM_A, SCORM_B)
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "wrong")
    await sit(s, "final", "right")

    body = await closing_of(s)
    assert body["outcome"]["checkin"]["status"] == "not_passed"
    assert body["outcome"]["final"] == {
        "status": "passed",
        "reason": None,
        "attempts": {"used": 1, "max": 3},
        **score("100.00"),
    }
    assert refusal(await try_final(s))["reason"] == "already_passed"


async def test_three_failed_finals_exhaust_it_and_the_check_in_stays_consumed(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, uid("alumno1"), SCORM_A, SCORM_B)
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "wrong")
    for _ in range(3):
        assert (await sit(s, "final", "wrong"))["passed"] is False

    outcome = (await closing_of(s))["outcome"]
    assert outcome["final"] == {
        "status": "exhausted",
        "reason": None,
        "attempts": {"used": 3, "max": 3},
        **score("0.00"),
    }
    assert outcome["checkin"]["attempts"] == {"used": 1, "max": 1}
    assert outcome["checkin"]["status"] == "not_passed"
    r = await try_final(s)
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "no_attempts_left")


# --- other callers and maps -----------------------------------------------------------------------


async def test_an_editor_in_preview_has_no_results(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    e = await env["editor"]("editor-1")
    await sit(e, "check_in", "right")  # a trial run
    outcome = (await closing_of(e))["outcome"]
    assert outcome["checkin"]["status"] == "not_taken"
    assert outcome["final"]["status"] == "required"
    assert outcome["scorm_gate"]["reason"] is None


async def test_a_map_without_a_final_step_has_no_final_to_require(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, closing=None)
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "right")
    outcome = (await closing_of(s))["outcome"]
    assert outcome["final"]["status"] == "locked"
    assert outcome["final"]["reason"] == "not_enabled"


async def test_a_map_without_a_check_in_reports_none(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, final={"checkIn": None})
    outcome = (await closing_of(await learner(env, "alumno1")))["outcome"]
    assert outcome["checkin"] is None
    assert outcome["final"]["status"] == "locked"


async def test_the_outcome_follows_the_attempts_not_a_stored_state(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await learner(env, "alumno1")
    before = (await closing_of(s))["outcome"]
    again = (await closing_of(s))["outcome"]
    assert before == again  # reading it changes nothing
    # Another student's attempts are not this student's.
    other = await learner(env, "alumno2")
    await sit(other, "check_in", "right")
    assert (await closing_of(s))["outcome"] == before


# --- the gate off ---------------------------------------------------------------------------------

# The final's score and rule while no final was sat (the map's current rule).
NO_SCORE = {"percent": None, "pass_percent": 80, "pass_operator": "gt"}


def score(percent: str, pass_percent: int = 80, operator: str = "gt") -> dict[str, Any]:
    return {
        "percent": percent,
        "pass_percent": pass_percent,
        "pass_operator": operator,
    }


BEFORE_KEYS = {"enforced", "preview", "available", "reason", "progress", "final"}


async def test_with_the_gate_off_the_answer_has_no_outcome_and_the_same_keys(
    env: dict[str, Any], provider: FakeCompletionProvider
) -> None:
    await world(env)  # no `mode` fixture: CLOSING_GATE_MODE stays off
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "right")
    r = await s.get("/trainer/closing")
    body = r.json()
    assert set(body) == BEFORE_KEYS
    assert (body["enforced"], body["progress"], body["available"]) == (
        False,
        None,
        False,
    )
    assert set(body["final"]) == {
        "purpose",
        "title",
        "publication_state",
        "section_count",
        "item_count",
        "attempts",
        "state",
        "locked_reason",
        "in_progress",
        "best",
        "already_passed",
    }
    # The wire format is the compact JSON of before (no spaces, same key order).
    assert (
        r.content
        == json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    )
    assert list(body) == [
        "enforced",
        "preview",
        "available",
        "reason",
        "progress",
        "final",
    ]
    assert provider.calls == 0


async def test_enforced_answers_keep_every_existing_key_in_place(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    body = await closing_of(await learner(env, "alumno1"))
    assert list(body) == [*BEFORE_KEYS_ORDER, "outcome"]


BEFORE_KEYS_ORDER = ["enforced", "preview", "available", "reason", "progress", "final"]
