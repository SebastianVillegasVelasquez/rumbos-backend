"""`CLOSING_GATE_MODE=observe`: the journey is read from the attempts, the
attempt rules still apply at the start of a final, and the content is never
checked (no completion provider, no Moodle read).

Every test runs with a completion provider and a Moodle client that both fail:
an answer that needed either could not look like the ones asserted here."""

import logging
from typing import Any

import httpx
import pytest

from app.closing.providers import CompletionUnavailableError
from app.core.config import get_settings
from app.main import app
from app.moodle.cache import CachedMoodleClient
from app.moodle.exceptions import MoodleUnavailableError
from tests import test_closing_api as api
from tests.fakes import FakeCompletionProvider, InMemoryMoodleClient
from tests.test_assessment_api import exams, start
from tests.test_closing_outcome import BEFORE_KEYS_ORDER, sit

env = api.env
clock = api.clock
draft_content_is_served = api.draft_content_is_served
provider = api.provider
mode = api.mode

learner, closing_of, try_final = api.learner, api.closing_of, api.try_final
refusal = api.refusal

NOT_CHECKED = {"satisfied": None, "reason": "not_checked"}


class Moodle:
    """What the test can look at afterwards: how often Moodle was asked."""

    def __init__(self, provider: FakeCompletionProvider) -> None:
        self.provider = provider
        self.contents = InMemoryMoodleClient()

    def assert_never_asked(self) -> None:
        assert self.provider.calls == 0
        assert self.contents.calls == 0


async def observe(
    env: dict[str, Any],
    provider: FakeCompletionProvider,
    mode: Any,
    **world: Any,
) -> Moodle:
    await api.world(env, **world)
    mode("observe")
    moodle = Moodle(provider)
    provider.error = CompletionUnavailableError()
    moodle.contents.error = MoodleUnavailableError("down")
    app.state.moodle_client = CachedMoodleClient(
        moodle.contents, ttl_seconds=0, stale_max_seconds=0
    )
    return moodle


async def outcome_of(c: httpx.AsyncClient) -> dict[str, Any]:
    body = await closing_of(c)
    outcome: dict[str, Any] = body["outcome"]
    assert outcome["scorm_gate"] == NOT_CHECKED
    assert body["enforced"] is False and body["progress"] is None
    return outcome


# --- the journey ------------------------------------------------------------------------


async def test_a_student_who_has_sat_nothing(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    moodle = await observe(env, provider, mode)
    s = await learner(env, "alumno1")
    body = await closing_of(s)
    assert list(body) == [*BEFORE_KEYS_ORDER, "outcome"]
    assert (body["available"], body["reason"]) == (True, None)
    outcome = await outcome_of(s)
    assert outcome["checkin"]["status"] == "not_taken"
    # Nothing about the content: `required` although nothing was done.
    assert outcome["final"]["status"] == "required"
    assert outcome["final"]["reason"] is None
    assert outcome["final"]["attempts"] == {"used": 0, "max": 3}
    moodle.assert_never_asked()


async def test_a_passed_check_in_exempts_and_still_says_not_checked(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    moodle = await observe(env, provider, mode)
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "right")
    outcome = await outcome_of(s)
    assert outcome["checkin"]["status"] == "passed"
    assert outcome["final"]["status"] == "not_required_passed_checkin"
    detail = refusal(await try_final(s))
    assert detail["reason"] == "already_passed"
    assert (await closing_of(s))["final"]["attempts"]["used"] == 0
    moodle.assert_never_asked()


async def test_a_failed_check_in_lets_the_final_start_without_the_content(
    env: dict[str, Any],
    provider: FakeCompletionProvider,
    mode: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    moodle = await observe(env, provider, mode)
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "wrong")
    assert (await outcome_of(s))["final"]["status"] == "required"
    with caplog.at_level(logging.WARNING, logger="rumbos.closing"):
        first = await try_final(s)
    assert first.status_code == 201, first.text
    assert "CLOSING_GATE_MODE=observe" in caplog.text
    # An attempt in progress is resumed, and the journey still asks for the final.
    again = await try_final(s)
    assert (again.status_code, again.json()["attempt_id"]) == (
        200,
        first.json()["attempt_id"],
    )
    assert (await outcome_of(s))["final"]["status"] == "required"
    moodle.assert_never_asked()


async def test_a_student_without_a_moodle_user_id_is_not_held_back(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await observe(env, provider, mode)
    s = await env["student"]("alumno1")  # the launch carried no `uid`
    assert (await outcome_of(s))["final"]["status"] == "required"
    assert (await try_final(s)).status_code == 201


async def test_a_passed_final_is_refused_as_already_passed(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    moodle = await observe(env, provider, mode)
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "wrong")
    await sit(s, "final", "right")
    assert (await outcome_of(s))["final"]["status"] == "passed"
    assert refusal(await try_final(s))["reason"] == "already_passed"
    moodle.assert_never_asked()


async def test_three_failed_finals_exhaust_it(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    moodle = await observe(env, provider, mode)
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "wrong")
    for used in (1, 2, 3):
        await sit(s, "final", "wrong")
        final = (await outcome_of(s))["final"]
        assert final["attempts"] == {"used": used, "max": 3}
        assert final["status"] == ("exhausted" if used == 3 else "required")
    r = await try_final(s)
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "no_attempts_left")
    moodle.assert_never_asked()


async def test_without_the_exemption_a_passed_check_in_does_not_exempt(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await observe(env, provider, mode, final={"exemption": {"enabled": False}})
    s = await learner(env, "alumno1")
    await sit(s, "check_in", "right")
    assert (await outcome_of(s))["final"]["status"] == "required"
    assert (await try_final(s)).status_code == 201


# --- what else the guard leaves alone ---------------------------------------------------


async def test_an_unpublished_final_is_locked_and_the_start_is_the_plain_404(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any, draft_a1: None
) -> None:
    """`form_unavailable` is not one of observe's refusals: the start answers
    what it answers with the gate off."""
    await observe(env, provider, mode)
    base_settings = get_settings()
    app.dependency_overrides[get_settings] = lambda: base_settings.model_copy(
        update={"trainer_serve_drafts": False, "closing_gate_mode": "observe"}
    )
    s = await learner(env, "alumno1")
    final = (await outcome_of(s))["final"]
    assert (final["status"], final["reason"]) == ("locked", "form_unavailable")
    assert (await try_final(s)).status_code == 404


async def test_the_final_leaves_the_trainer_list_as_with_enforce(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await observe(env, provider, mode)
    s = await learner(env, "alumno1")
    items = (await s.get("/trainer/assessments")).json()["items"]
    assert [i["purpose"] for i in items] == ["check_in"]


async def test_a_map_without_a_final_step_is_not_gated_and_keeps_its_list(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    """The transitional rule (D53) holds in observe too: not even a passed
    check-in is refused there."""
    await observe(env, provider, mode, closing=None)
    s = await learner(env, "alumno1")
    items = (await s.get("/trainer/assessments")).json()["items"]
    assert [i["purpose"] for i in items] == ["check_in", "final"]
    await sit(s, "check_in", "right")
    assert (await outcome_of(s))["final"]["status"] == "locked"
    assert (await try_final(s)).status_code == 201


async def test_an_editor_in_preview_has_a_trial_run_and_no_results(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    moodle = await observe(env, provider, mode)
    e = await env["editor"]("editor-1")
    await sit(e, "check_in", "right")
    outcome = await outcome_of(e)
    assert outcome["checkin"]["status"] == "not_taken"
    assert outcome["final"]["status"] == "required"
    assert (await start(e, "final"))["preview"] is True
    moodle.assert_never_asked()


async def test_the_check_in_is_not_gated(
    env: dict[str, Any], provider: FakeCompletionProvider, mode: Any
) -> None:
    await observe(env, provider, mode, final={"checkIn": exams()["checkIn"]})
    s = await learner(env, "alumno1")
    assert (await start(s, "check_in"))["_status"] == 201
