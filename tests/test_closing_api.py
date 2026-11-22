"""The final exam behind the closing gate, through the real stack.

`GET /trainer/closing` and `POST /trainer/assessments/final/attempts` answer
from one rule, so every case here is checked on both: what the screen is told
and what a direct API call gets.
"""

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.closing.providers import CompletionUnavailableError
from app.core.config import get_settings
from app.main import app
from app.models import PassbackOutbox
from app.moodle.cache import CachedMoodleClient
from app.moodle.exceptions import MoodleUnavailableError
from app.moodle.schemas import MoodleModule, MoodleSection
from tests import test_assessment_api as base
from tests.fakes import FakeCompletionProvider, InMemoryMoodleClient
from tests.test_assessment_api import COURSE, exams, put, start
from tests.test_publications_api import add_bubble, make_map, publish
from tests.test_welcome_api import put_settings

# The exam's own fixtures (a published map, students, editors, a fake clock).
env = base.env
clock = base.clock
draft_content_is_served = base.draft_content_is_served

SCORM_A, SCORM_B, QUIZ, HIDDEN_SCORM = 101, 102, 103, 104
CLOSING = {
    "steps": [
        {"type": "html", "key": "cierre01", "title": "Cierre", "html": "<p>Fin</p>"},
        {
            "type": "assessment",
            "key": "examenfin",
            "title": "Examen",
            "purpose": "final",
        },
    ]
}


def mod(cmid: int, modname: str, **over: Any) -> MoodleModule:
    return MoodleModule(
        id=cmid,
        name=f"A{cmid}",
        modname=modname,
        url=f"https://m/mod/{modname}/view.php?id={cmid}",
        completion=2,
        **over,
    )


def course(*modules: MoodleModule) -> list[MoodleSection]:
    return [MoodleSection(id=1, section=1, modules=list(modules))]


@pytest.fixture
def provider() -> Iterator[FakeCompletionProvider]:
    fake = FakeCompletionProvider()
    app.state.completion_provider = fake
    yield fake
    app.state.completion_provider = None


@pytest.fixture
def mode() -> Iterator[Any]:
    """Sets CLOSING_GATE_MODE for the test (the default of these tests is on)."""

    def set_mode(value: str) -> None:
        base = get_settings()
        app.dependency_overrides[get_settings] = lambda: base.model_copy(
            update={"trainer_serve_drafts": True, "closing_gate_mode": value}
        )

    set_mode("enforce")
    yield set_mode


async def world(
    env: dict[str, Any],
    *,
    modules: list[MoodleModule] | None = None,
    bubbles: tuple[int, ...] = (SCORM_A, SCORM_B, QUIZ),
    final: dict[str, Any] | None = None,
    closing: dict[str, Any] | None = CLOSING,
) -> None:
    """A published map with the given bubbles, the exams and a closing flow."""
    fake = InMemoryMoodleClient()
    fake.courses[COURSE] = course(
        *(
            modules
            if modules is not None
            else [
                mod(SCORM_A, "scorm"),
                mod(SCORM_B, "scorm"),
                mod(QUIZ, "quiz"),
                mod(HIDDEN_SCORM, "scorm", visible=False),
            ]
        )
    )
    app.state.moodle_client = CachedMoodleClient(
        fake, ttl_seconds=0, stale_max_seconds=0
    )
    editor = env["client"]()
    created = await make_map(editor, course=COURSE)
    for activity in bubbles:
        await add_bubble(editor, created["id"], activity)
    settings: dict[str, Any] = {"assessments": exams(**(final or {}))}
    if closing is not None:
        settings["closing"] = closing
    r = await put_settings(editor, created["id"], settings)
    assert r.status_code == 200, r.text
    await publish(editor, created["id"])


async def closing_of(c: httpx.AsyncClient) -> dict[str, Any]:
    r = await c.get("/trainer/closing")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def try_final(c: httpx.AsyncClient) -> httpx.Response:
    return await c.post("/trainer/assessments/final/attempts")


def refusal(r: httpx.Response) -> dict[str, Any]:
    assert r.status_code == 409, r.text
    detail: dict[str, Any] = r.json()["detail"]
    assert detail["code"] == "closing_not_available"
    assert detail["message_key"] == f"closing.not_available.{detail['reason']}"
    return detail


# --- the screen and the server agree ------------------------------------------------------


async def test_locked_then_available_as_the_scorm_is_completed(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await env["student"]("alumno1")
    body = await closing_of(s)
    assert body["enforced"] and not body["preview"]
    assert (body["available"], body["reason"]) == (False, "scorm_incomplete")
    assert body["progress"] == {"completed": 0, "total": 2}
    assert body["final"]["purpose"] == "final"
    assert body["final"]["attempts"] == {"used": 0, "max": 3}

    provider.complete(COURSE, "alumno1", SCORM_A)
    assert (await closing_of(s))["progress"] == {"completed": 1, "total": 2}
    assert not (await closing_of(s))["available"]

    provider.complete(COURSE, "alumno1", SCORM_B)
    body = await closing_of(s)
    assert (body["available"], body["reason"]) == (True, None)
    assert body["progress"] == {"completed": 2, "total": 2}
    started = await try_final(s)
    assert started.status_code == 201, started.text


async def test_a_direct_start_without_completion_is_refused_and_records_nothing(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await env["student"]("alumno1")
    detail = refusal(await try_final(s))
    assert (detail["reason"], detail["completed"], detail["total"]) == (
        "scorm_incomplete",
        0,
        2,
    )
    provider.complete(COURSE, "alumno1", SCORM_A)
    detail = refusal(await try_final(s))
    assert (detail["completed"], detail["total"]) == (1, 2)
    assert (await closing_of(s))["final"]["attempts"] == {"used": 0, "max": 3}


async def test_the_refusal_names_no_activity_and_no_internals(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await env["student"]("alumno1")
    detail = refusal(await try_final(s))
    assert set(detail) == {
        "code",
        "message_key",
        "message",
        "reason",
        "completed",
        "total",
    }


async def test_completion_is_per_student(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    done = await env["student"]("alumno1")
    other = await env["student"]("alumno2")
    assert (await try_final(other)).status_code == 409
    assert (await try_final(done)).status_code == 201
    assert not (await closing_of(other))["available"]


async def test_a_revoked_completion_closes_the_exam_again(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await env["student"]("alumno1")
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    assert (await closing_of(s))["available"]
    provider.revoke(COURSE, "alumno1", SCORM_B)
    assert refusal(await try_final(s))["reason"] == "scorm_incomplete"
    assert not (await closing_of(s))["available"]


async def test_a_hidden_scorm_does_not_count(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, bubbles=(SCORM_A, HIDDEN_SCORM))
    s = await env["student"]("alumno1")
    assert (await closing_of(s))["progress"] == {"completed": 0, "total": 1}
    provider.complete(COURSE, "alumno1", SCORM_A)
    assert (await try_final(s)).status_code == 201


async def test_a_map_with_no_scorm_opens_the_exam_at_once(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, bubbles=(QUIZ,))
    s = await env["student"]("alumno1")
    body = await closing_of(s)
    assert body["available"] and body["progress"] == {"completed": 0, "total": 0}
    assert (await try_final(s)).status_code == 201
    assert provider.calls == 0  # nothing to ask Moodle


async def test_the_screen_and_the_server_agree_on_every_case(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    for sub, done, broken in [
        ("a", (), False),
        ("b", (SCORM_A,), False),
        ("c", (SCORM_A, SCORM_B), False),
        ("d", (SCORM_A, SCORM_B), True),
    ]:
        provider.complete(COURSE, sub, *done)
        provider.error = CompletionUnavailableError() if broken else None
        s = await env["student"](sub)
        shown = await closing_of(s)
        r = await try_final(s)
        assert (r.status_code == 201) == shown["available"], sub
        if not shown["available"]:
            assert refusal(r)["reason"] == shown["reason"], sub


async def test_an_unreadable_completion_closes_the_exam(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    provider.error = CompletionUnavailableError()
    s = await env["student"]("alumno1")
    assert (await closing_of(s))["reason"] == "progress_unavailable"
    assert refusal(await try_final(s))["reason"] == "progress_unavailable"


async def test_unreadable_course_contents_close_the_exam(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    app.state.moodle_client = CachedMoodleClient(
        _failing(), ttl_seconds=0, stale_max_seconds=0
    )
    s = await env["student"]("alumno1")
    assert refusal(await try_final(s))["reason"] == "progress_unavailable"


def _failing() -> InMemoryMoodleClient:
    fake = InMemoryMoodleClient()
    fake.error = MoodleUnavailableError("down")
    return fake


async def test_with_no_provider_configured_enforce_keeps_the_exam_closed(
    env: dict[str, Any], mode: Any
) -> None:
    await world(env)
    s = await env["student"]("alumno1")
    assert refusal(await try_final(s))["reason"] == "progress_unavailable"


# --- the other conditions -----------------------------------------------------------------


async def test_no_closing_flow_or_no_final_step_means_no_exam(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, closing=None)
    s = await env["student"]("alumno1")
    assert (await closing_of(s))["reason"] == "not_enabled"
    assert refusal(await try_final(s))["reason"] == "not_enabled"


async def test_a_closing_flow_without_a_final_step_means_no_exam(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    only_content = {"steps": [CLOSING["steps"][0]]}
    await world(env, closing=only_content)
    s = await env["student"]("alumno1")
    assert (await closing_of(s))["reason"] == "no_final_step"
    assert refusal(await try_final(s))["reason"] == "no_final_step"


async def test_attempts_exhausted_keeps_the_existing_error(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, final={"final": {"formId": "a1-form-a", "maxAttempts": 1}})
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    s = await env["student"]("alumno1")
    attempt = (await start(s, "final"))["attempt_id"]
    await put(s, attempt, "wrong")
    assert (await s.post(f"/trainer/attempts/{attempt}/submit")).json()[
        "passed"
    ] is False
    assert (await closing_of(s))["reason"] == "no_attempts_left"
    r = await try_final(s)
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "no_attempts_left")


async def test_a_passed_student_is_not_asked_to_attempt_again(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    s = await env["student"]("alumno1")
    attempt = (await start(s, "final"))["attempt_id"]
    await put(s, attempt, "right")
    assert (await s.post(f"/trainer/attempts/{attempt}/submit")).json()["passed"]
    body = await closing_of(s)
    assert (body["available"], body["reason"]) == (False, "already_passed")
    assert body["final"]["already_passed"] is True
    assert refusal(await try_final(s))["reason"] == "already_passed"
    assert (await closing_of(s))["final"]["attempts"]["used"] == 1


async def test_a_check_in_pass_with_the_exemption_counts_as_passed(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    s = await env["student"]("alumno1")
    attempt = (await start(s))["attempt_id"]
    await put(s, attempt, "right")
    await s.post(f"/trainer/attempts/{attempt}/submit")
    assert refusal(await try_final(s))["reason"] == "already_passed"


async def test_the_check_in_is_not_gated(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await env["student"]("alumno1")
    assert (await s.post("/trainer/assessments/check_in/attempts")).status_code == 201


async def test_a_started_attempt_can_be_resumed_and_finished(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    s = await env["student"]("alumno1")
    first = (await try_final(s)).json()["attempt_id"]
    again = await try_final(s)
    assert (again.status_code, again.json()["attempt_id"]) == (200, first)


# --- the final leaves the trainer list; the gate switch -------------------------------------


async def test_the_final_is_not_listed_in_the_trainer_when_enforced(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    s = await env["student"]("alumno1")
    items = (await s.get("/trainer/assessments")).json()["items"]
    assert [i["purpose"] for i in items] == ["check_in"]


async def test_the_gate_is_off_by_default_and_warns(
    env: dict[str, Any], provider: FakeCompletionProvider, caplog: Any
) -> None:
    await world(env)  # no `mode` fixture: the setting keeps its default
    s = await env["student"]("alumno1")
    items = (await s.get("/trainer/assessments")).json()["items"]
    assert [i["purpose"] for i in items] == ["check_in", "final"]
    with caplog.at_level(logging.WARNING, logger="rumbos.closing"):
        r = await try_final(s)  # nothing completed, and it still starts
    assert r.status_code == 201
    assert "CLOSING_GATE_MODE=off" in caplog.text
    body = await closing_of(s)
    assert body["enforced"] is False and body["progress"] is None
    assert provider.calls == 0


async def test_old_final_attempts_stay_readable_after_enforcing(
    env: dict[str, Any], provider: FakeCompletionProvider
) -> None:
    await world(env)
    s = await env["student"]("alumno1")
    attempt = (await try_final(s)).json()["attempt_id"]  # gate off: allowed
    await put(s, attempt, "right")
    before = (await s.post(f"/trainer/attempts/{attempt}/submit")).json()
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": True, "closing_gate_mode": "enforce"}
    )
    after = await s.get(f"/trainer/attempts/{attempt}/result")
    assert after.status_code == 200 and after.json() == before
    assert (await closing_of(s))["final"]["best"]["passed"] is True


# --- an editor's trial run --------------------------------------------------------------------


async def outbox_rows(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            await conn.scalar(select(func.count()).select_from(PassbackOutbox)) or 0
        )


async def test_an_editor_can_run_the_final_without_the_gate(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    e = await env["editor"]("editor-1")
    body = await closing_of(e)
    assert (body["preview"], body["available"], body["reason"]) == (True, True, None)
    assert provider.calls == 0
    started = await try_final(e)
    assert started.status_code == 201 and started.json()["preview"] is True


async def test_a_trial_run_leaves_no_trace_in_real_data(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, final={"final": {"formId": "a1-form-a", "maxAttempts": 1}})
    e = await env["editor"]("editor-1")
    rows_before = await outbox_rows(env["engine"])
    for _ in range(3):  # more than the maximum: the counters do not apply
        attempt = (await try_final(e)).json()["attempt_id"]
        await put(e, attempt, "right")
        result = (await e.post(f"/trainer/attempts/{attempt}/submit")).json()
        assert result["preview"] is True and result["sync"]["state"] == "not_required"
    assert await outbox_rows(env["engine"]) == rows_before
    # The same person as a student: nothing counted, nothing passed.
    as_student = await env["student"]("editor-1")
    assert (await closing_of(as_student))["final"]["attempts"] == {"used": 0, "max": 1}
    assert (await closing_of(as_student))["final"]["already_passed"] is False


async def test_a_real_attempt_says_it_is_not_a_preview(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    s = await env["student"]("alumno1")
    started = (await try_final(s)).json()
    assert started["preview"] is False
    result = (await s.post(f"/trainer/attempts/{started['attempt_id']}/submit")).json()
    assert result["preview"] is False


async def test_an_editor_cannot_read_a_students_attempt(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env)
    provider.complete(COURSE, "alumno1", SCORM_A, SCORM_B)
    s = await env["student"]("alumno1")
    attempt = (await try_final(s)).json()["attempt_id"]
    e = await env["editor"]("editor-1")
    assert (
        await e.get(f"/trainer/attempts/{attempt}/sections/reading")
    ).status_code == 404
    assert (await e.get(f"/trainer/attempts/{uuid.uuid4()}/result")).status_code == 404


async def test_preview_still_needs_the_flow_configured(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await world(env, closing=None)
    e = await env["editor"]("editor-1")
    assert (await closing_of(e))["reason"] == "not_enabled"
    assert refusal(await try_final(e))["reason"] == "not_enabled"
