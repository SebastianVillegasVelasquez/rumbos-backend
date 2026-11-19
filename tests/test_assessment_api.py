"""The native exam through the real stack: students, editors in preview, the
server clock, the attempt limits and what never leaves the server."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assessment.access import get_clock
from app.assessment.metrics import METRICS
from app.auth.tokens import hash_token, new_token
from app.core.config import get_settings
from app.enums import Role
from app.main import app
from app.models import AccessToken
from app.models.tenant import LEGACY_TENANT_ID
from tests.editor_client import EditorClient
from tests.fakes import InMemoryMoodleClient
from tests.tenant_helpers import insert_tenant
from tests.test_assessment_engine import answers as engine_answers
from tests.test_publications_api import make_map, publish
from tests.test_welcome_api import put_settings

START = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
COURSE = 7


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def exams(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "checkIn": {"formId": "a1-form-a", "maxAttempts": 1, "timeLimitMin": 60},
        "final": {"formId": "a1-form-a", "maxAttempts": 3, "timeLimitMin": 60},
        "passPercent": 80,
        "passOperator": "gt",
        "exemption": {"enabled": True},
    }
    return {**base, **over}


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    fake = FakeClock()
    app.dependency_overrides[get_clock] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_clock, None)


@pytest.fixture(autouse=True)
def draft_content_is_served() -> Any:
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": True}
    )
    METRICS.reset()
    app.state.trainer_limiter = None
    yield
    app.dependency_overrides.pop(get_settings, None)


async def token(
    engine: AsyncEngine,
    role: Role,
    sub: str,
    tenant: uuid.UUID = LEGACY_TENANT_ID,
    course: int = COURSE,
) -> str:
    secret = new_token()
    async with engine.begin() as conn:
        await conn.execute(
            insert(AccessToken).values(
                token_hash=hash_token(secret),
                tenant_id=tenant,
                course_id=course,
                role=role,
                sub=sub,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                created_at=datetime.now(UTC),
            )
        )
    return secret


@pytest.fixture
async def env(engine: AsyncEngine, clock: FakeClock) -> AsyncIterator[dict[str, Any]]:
    """A published map with the exams on, a student, and an editor."""
    app.state.db_engine = engine
    app.state.moodle_client = InMemoryMoodleClient()
    transport = httpx.ASGITransport(app=app)
    clients: list[EditorClient] = []

    def client(secret: str | None = None) -> EditorClient:
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        c = EditorClient(
            transport=transport, base_url="http://t/api/v1", headers=headers
        )
        clients.append(c)
        return c

    async def setup(settings: dict[str, Any]) -> None:
        legacy = client()  # anonymous legacy editor (dev mode)
        created = await make_map(legacy, course=COURSE)
        r = await put_settings(legacy, created["id"], settings)
        assert r.status_code == 200, r.text
        await publish(legacy, created["id"])

    async def student(sub: str = "student-1", **kw: Any) -> EditorClient:
        return client(await token(engine, Role.STUDENT, sub, **kw))

    async def editor(sub: str = "editor-1") -> EditorClient:
        return client(await token(engine, Role.EDITOR, sub))

    yield {
        "setup": setup,
        "student": student,
        "editor": editor,
        "engine": engine,
        "client": client,
    }
    for c in clients:
        await c.aclose()


async def start(c: httpx.AsyncClient, purpose: str = "check_in") -> dict[str, Any]:
    r = await c.post(f"/trainer/assessments/{purpose}/attempts")
    assert r.status_code in (200, 201), r.text
    body: dict[str, Any] = r.json()
    body["_status"] = r.status_code
    return body


async def put(c: httpx.AsyncClient, attempt: str, kind: str) -> httpx.Response:
    items = engine_answers(kind)
    # Objective answers only: the helper uses the real keys, the server never does.
    payload = [{"item_id": k, "response": v} for k, v in items.items()]
    return await c.put(
        f"/trainer/attempts/{attempt}/answers", json={"answers": payload}
    )


# --- off by default ---------------------------------------------------------------


async def test_everything_is_off_until_a_map_enables_it(env: dict[str, Any]) -> None:
    await env["setup"]({})
    s = await env["student"]()
    assert (await s.get("/trainer/assessments")).json() == {"enabled": False}
    r = await s.post("/trainer/assessments/check_in/attempts")
    assert r.status_code == 404


async def test_authentication_is_required(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    anonymous = env["client"]("x" * 43)
    assert (await anonymous.get("/trainer/assessments")).status_code == 401


# --- listing and starting -----------------------------------------------------------


async def test_the_list_shows_policy_attempts_and_state(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    body = (await s.get("/trainer/assessments")).json()
    assert body["enabled"] and not body["preview"]
    assert body["policy"] == {
        "pass_percent": 80,
        "pass_operator": "gt",
        "time_limit_min": 60,
    }
    check_in, final = body["items"]
    assert (check_in["purpose"], check_in["attempts"], check_in["state"]) == (
        "check_in",
        {"used": 0, "max": 1},
        "available",
    )
    assert (final["purpose"], final["attempts"]) == ("final", {"used": 0, "max": 3})
    assert check_in["item_count"] == 45 and check_in["section_count"] == 5
    assert check_in["in_progress"] is None and check_in["best"] is None


async def test_start_is_201_then_resume_is_200_with_the_same_attempt(
    env: dict[str, Any], clock: FakeClock
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    first = await start(s)
    assert first["_status"] == 201
    assert first["deadline_at"].startswith("2026-11-17T13:00:00")
    assert first["server_now"].startswith("2026-11-17T12:00:00")
    assert [x["key"] for x in first["sections"]] == [
        "listening",
        "reading",
        "use_of_english",
        "writing",
        "speaking",
    ]
    clock.advance(minutes=5)
    again = await start(s)
    assert again["_status"] == 200 and again["attempt_id"] == first["attempt_id"]
    assert again["deadline_at"] == first["deadline_at"]  # the clock did not restart
    listing = (await s.get("/trainer/assessments")).json()["items"][0]
    assert listing["state"] == "in_progress"
    assert listing["in_progress"]["attempt_id"] == first["attempt_id"]


async def test_two_tabs_cannot_consume_two_attempts(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    results = await asyncio.gather(
        *(s.post("/trainer/assessments/check_in/attempts") for _ in range(4))
    )
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1 and all(c in (200, 201) for c in codes), codes
    assert len({r.json()["attempt_id"] for r in results}) == 1
    assert (await s.get("/trainer/assessments")).json()["items"][0]["attempts"][
        "used"
    ] == 1


async def test_unknown_purpose_is_404(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    assert (await s.post("/trainer/assessments/bonus/attempts")).status_code == 404


async def test_a_draft_form_is_not_served_in_production(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": False}
    )
    s = await env["student"]()
    item = (await s.get("/trainer/assessments")).json()["items"][0]
    assert (item["state"], item["locked_reason"]) == ("locked", "not_available")
    assert (await s.post("/trainer/assessments/check_in/attempts")).status_code == 404


# --- sections: nothing secret, the same shuffle on reload ---------------------------


FORBIDDEN = {
    "key",
    "correct",
    "transcript",
    "rubric",
    "accepted",
    "competence",
    "source_rubric",
}


def walk_items(section: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for p in section["parts"] for i in p["items"]]


async def test_a_section_carries_no_key_and_resumes_in_the_same_order(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    texts = []
    for key in ("listening", "reading", "use_of_english", "writing", "speaking"):
        r = await s.get(f"/trainer/attempts/{a}/sections/{key}")
        assert r.status_code == 200, r.text
        section = r.json()
        texts.append(json.dumps(section, ensure_ascii=False))
        assert section["saved"] == {} and section["key"] == key
        for item in walk_items(section):
            assert not FORBIDDEN & {k for k, v in item.items() if v is not None}, item[
                "item_id"
            ]
        again = (await s.get(f"/trainer/attempts/{a}/sections/{key}")).json()
        assert [i["item_id"] for i in walk_items(again)] == [
            i["item_id"] for i in walk_items(section)
        ]
    blob = "\n".join(texts)
    assert "✓" not in blob and "Good afternoon, sir" not in blob  # no transcripts


async def test_a_second_attempt_shuffles_differently(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    orders = []
    for _ in range(2):
        a = (await start(s, "final"))["attempt_id"]
        sec = (await s.get(f"/trainer/attempts/{a}/sections/reading")).json()
        orders.append([i["item_id"] for i in walk_items(sec)])
        await s.post(f"/trainer/attempts/{a}/submit")
    assert orders[0] != orders[1]


async def test_an_unknown_section_or_attempt_is_404(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    assert (await s.get(f"/trainer/attempts/{a}/sections/nope")).status_code == 404
    assert (
        await s.get(f"/trainer/attempts/{uuid.uuid4()}/sections/reading")
    ).status_code == 404


# --- autosave -----------------------------------------------------------------------


async def test_answers_autosave_and_a_reload_shows_them(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    section = (await s.get(f"/trainer/attempts/{a}/sections/reading")).json()
    item = walk_items(section)[0]
    first, second = item["options"][0]["option_id"], item["options"][1]["option_id"]
    for chosen in (first, second):  # last write wins
        r = await s.put(
            f"/trainer/attempts/{a}/answers",
            json={
                "answers": [
                    {"item_id": item["item_id"], "response": {"option_id": chosen}}
                ]
            },
        )
        assert r.status_code == 200, r.text
    reloaded = (await s.get(f"/trainer/attempts/{a}/sections/reading")).json()
    assert reloaded["saved"][item["item_id"]] == {"option_id": second}


@pytest.mark.parametrize(
    ("item_id", "response", "status"),
    [
        ("reading.a.01", {"option_id": "a"}, 422),
        ("reading.a.01", {}, 422),
        ("nope.item", {"option_id": "x"}, 404),
        ("writing.tasks.01", {"values": {"hacker": "x"}}, 422),
    ],
)
async def test_bad_answers_are_refused(
    env: dict[str, Any], item_id: str, response: dict[str, Any], status: int
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    r = await s.put(
        f"/trainer/attempts/{a}/answers",
        json={"answers": [{"item_id": item_id, "response": response}]},
    )
    assert r.status_code == status


async def test_nobody_else_can_touch_my_attempt(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    mine = await env["student"]("alice")
    other = await env["student"]("bob")
    a = (await start(mine))["attempt_id"]
    assert (
        await other.get(f"/trainer/attempts/{a}/sections/reading")
    ).status_code == 404
    assert (
        await other.put(f"/trainer/attempts/{a}/answers", json={"answers": []})
    ).status_code == 404
    assert (await other.post(f"/trainer/attempts/{a}/submit")).status_code == 404
    assert (await other.get(f"/trainer/attempts/{a}/result")).status_code == 404


# --- submit and results ---------------------------------------------------------------


async def test_a_perfect_attempt_passes_and_is_idempotent(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    assert (await put(s, a, "right")).status_code == 200
    r = await s.post(f"/trainer/attempts/{a}/submit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["percent"], body["passed"], body["band"]) == (
        "submitted",
        "100.00",
        True,
        "achieved",
    )
    assert body["attempts"] == {"used": 1, "max": 1}
    assert body["sync"] == {"state": "pending"} and body["gain"] is None
    assert {x["key"] for x in body["by_section"]} == {
        "listening",
        "reading",
        "use_of_english",
        "writing",
        "speaking",
    }
    assert {m["module"] for m in body["by_module"]} == {"M1", "M2", "M3", "M4"}
    assert (await s.post(f"/trainer/attempts/{a}/submit")).json() == body
    assert (await s.get(f"/trainer/attempts/{a}/result")).json() == body
    # No per-item verdicts anywhere in a result.
    assert "items" not in body
    # Closed: no more saves, no more reading the items.
    assert (await put(s, a, "right")).status_code == 409
    assert (await s.get(f"/trainer/attempts/{a}/sections/reading")).json()["detail"][
        "code"
    ] == "submitted"
    # A pass locks the exam, and the list says so.
    assert (await s.post("/trainer/assessments/check_in/attempts")).json()["detail"][
        "code"
    ] == "locked"
    listed = (await s.get("/trainer/assessments")).json()["items"][0]
    assert listed["state"] == "passed" and listed["best"]["percent"] == "100.00"


async def test_a_failed_check_in_uses_its_only_attempt(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    await put(s, a, "wrong")
    body = (await s.post(f"/trainer/attempts/{a}/submit")).json()
    assert (body["percent"], body["passed"], body["band"]) == (
        "0.00",
        False,
        "emerging",
    )
    r = await s.post("/trainer/assessments/check_in/attempts")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_attempts_left"
    assert (await s.get("/trainer/assessments")).json()["items"][0][
        "state"
    ] == "exhausted"


async def test_the_final_has_three_attempts_and_reports_the_gain(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    ci = (await start(s))["attempt_id"]
    await put(s, ci, "wrong")
    await s.post(f"/trainer/attempts/{ci}/submit")
    for n in range(3):
        a = (await start(s, "final"))["attempt_id"]
        await put(s, a, "wrong" if n < 2 else "right")
        body = (await s.post(f"/trainer/attempts/{a}/submit")).json()
        assert body["attempts"] == {"used": n + 1, "max": 3}
        assert body["gain"] == body["percent"]  # check-in was 0.00
    assert body["passed"] is True
    # Three used: a fourth would be refused (even without the pass lock).
    r = await s.post("/trainer/assessments/final/attempts")
    assert r.status_code == 409


@pytest.mark.parametrize(("operator", "passed"), [("gt", False), ("gte", True)])
async def test_the_pass_operator_decides_the_boundary_exactly(
    env: dict[str, Any], operator: str, passed: bool
) -> None:
    """Objective items all right and no Writing or Speaking is exactly 70.00."""
    await env["setup"]({"assessments": exams(passPercent=70, passOperator=operator)})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    objective = [
        {"item_id": k, "response": v}
        for k, v in engine_answers("right").items()
        if "values" not in v
    ]
    await s.put(f"/trainer/attempts/{a}/answers", json={"answers": objective})
    body = (await s.post(f"/trainer/attempts/{a}/submit")).json()
    assert body["percent"] == "70.00" and body["passed"] is passed


# --- the server clock ------------------------------------------------------------------


async def test_an_expired_attempt_is_closed_with_what_was_saved(
    env: dict[str, Any], clock: FakeClock
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    objective = [
        {"item_id": k, "response": v}
        for k, v in engine_answers("right").items()
        if "values" not in v
    ]
    await s.put(f"/trainer/attempts/{a}/answers", json={"answers": objective})
    clock.advance(minutes=59, seconds=59)
    assert (await put(s, a, "right")).status_code == 200  # still inside
    clock.advance(seconds=2)  # one second past the deadline
    late = await put(s, a, "wrong")
    assert late.status_code == 409 and late.json()["detail"]["code"] == "expired"
    result = (await s.get(f"/trainer/attempts/{a}/result")).json()
    assert result["status"] == "expired"
    assert result["percent"] == "100.00"  # what was saved before the deadline
    assert (await s.get(f"/trainer/attempts/{a}/sections/reading")).json()["detail"][
        "code"
    ] == "expired"
    assert (await s.get("/trainer/assessments")).json()["items"][0]["state"] in (
        "exhausted",
        "passed",
    )


async def test_submitting_after_the_deadline_is_an_expiry(
    env: dict[str, Any], clock: FakeClock
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    await put(s, a, "right")
    clock.advance(minutes=61)
    assert (await s.post(f"/trainer/attempts/{a}/submit")).json()["status"] == "expired"


async def test_a_result_is_not_available_while_in_progress(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    r = await s.get(f"/trainer/attempts/{a}/result")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "in_progress"


async def test_a_deadline_that_passed_frees_the_slot_for_a_new_attempt(
    env: dict[str, Any], clock: FakeClock
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    first = (await start(s, "final"))["attempt_id"]
    clock.advance(hours=2)
    second = await start(s, "final")
    assert second["_status"] == 201 and second["attempt_id"] != first


# --- preview ----------------------------------------------------------------------------


async def test_preview_runs_everything_without_consuming_real_attempts(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    ed = await env["editor"]()
    assert (await ed.get("/trainer/assessments")).json()["preview"] is True
    for _ in range(3):  # more than the 1 attempt of a student
        a = (await start(ed))["attempt_id"]
        await put(ed, a, "right")
        assert (await ed.post(f"/trainer/attempts/{a}/submit")).json()["passed"] is True
    student = await env["student"]()
    item = (await student.get("/trainer/assessments")).json()["items"][0]
    assert item["attempts"]["used"] == 0 and item["state"] == "available"


async def test_preview_attempts_are_invisible_to_students_and_vice_versa(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    ed = await env["editor"]("same-sub")
    st = await env["student"]("same-sub")
    a = (await start(ed))["attempt_id"]
    assert (await st.get(f"/trainer/attempts/{a}/sections/reading")).status_code == 404


# --- tenants and limits --------------------------------------------------------------------


async def test_another_tenants_user_cannot_see_the_exam_or_the_attempt(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    mine = await env["student"]()
    a = (await start(mine))["attempt_id"]
    other_tenant = await insert_tenant(env["engine"], "other")
    stranger = await env["student"]("stranger", tenant=other_tenant)
    assert (await stranger.get("/trainer/assessments")).json() == {"enabled": False}
    assert (
        await stranger.get(f"/trainer/attempts/{a}/sections/reading")
    ).status_code == 404


async def test_another_course_sees_no_exam(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    elsewhere = await env["student"]("zed", course=COURSE + 1)
    assert (await elsewhere.get("/trainer/assessments")).json() == {"enabled": False}


async def test_a_user_who_sends_too_many_requests_gets_429(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": True, "trainer_rate_per_minute": 3}
    )
    s = await env["student"]()
    codes = [(await s.get("/trainer/assessments")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    r = await s.get("/trainer/assessments")
    assert r.headers["retry-after"].isdigit()


async def test_metrics_count_what_happened(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    await put(s, a, "right")
    await s.post(f"/trainer/attempts/{a}/submit")
    snap = METRICS.snapshot()
    assert snap["started.check_in.real"] == 1
    assert snap["submitted.check_in.real"] == 1
    assert snap["band.achieved.real"] == 1


async def test_logs_never_carry_student_text(
    env: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    secret = "MySecretAnswer"
    await s.put(
        f"/trainer/attempts/{a}/answers",
        json={
            "answers": [
                {
                    "item_id": "writing.tasks.01",
                    "response": {"values": {"name": secret}},
                }
            ]
        },
    )
    with caplog.at_level("DEBUG"):
        await s.post(f"/trainer/attempts/{a}/submit")
    assert secret not in caplog.text
    assert "attempt_closed" in caplog.text


async def test_an_editor_previews_a_draft_form_a_student_does_not(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"trainer_serve_drafts": False}  # as in production
    )
    ed = await env["editor"]()
    item = (await ed.get("/trainer/assessments")).json()["items"][0]
    assert item["publication_state"] == "draft"
    assert item["state"] == "available"
    assert (await ed.post("/trainer/assessments/check_in/attempts")).status_code == 201
    s = await env["student"]()
    seen = (await s.get("/trainer/assessments")).json()["items"][0]
    assert (seen["state"], seen["locked_reason"]) == ("locked", "not_available")
    assert "publication_state" not in seen


# --- forms ----------------------------------------------------------------------------


async def test_an_editor_lists_the_forms_with_their_state(env: dict[str, Any]) -> None:
    ed = await env["editor"]()
    r = await ed.get("/trainer/assessments/forms")
    assert r.status_code == 200
    forms = {f["id"]: f for f in r.json()}
    assert set(forms["a1-form-a"]) == {
        "id",
        "title",
        "language",
        "level",
        "purpose",
        "publication_state",
    }
    assert forms["a1-form-a"]["publication_state"] in {"draft", "reviewed", "published"}
    assert [f["id"] for f in r.json()] == sorted(forms)


async def test_the_forms_list_is_for_editors_only(env: dict[str, Any]) -> None:
    s = await env["student"]()
    assert (await s.get("/trainer/assessments/forms")).status_code == 403
    unknown = env["client"]("x" * 43)
    assert (await unknown.get("/trainer/assessments/forms")).status_code == 401


# --- already passed (the closing flow's final-exam step) ---------------------------------


async def pass_exam(c: httpx.AsyncClient, purpose: str) -> None:
    a = (await start(c, purpose))["attempt_id"]
    await put(c, a, "right")
    assert (await c.post(f"/trainer/attempts/{a}/submit")).json()["passed"] is True


async def passed_flags(c: httpx.AsyncClient) -> dict[str, Any]:
    items = (await c.get("/trainer/assessments")).json()["items"]
    return {i["purpose"]: i["already_passed"] for i in items}


async def test_nobody_has_passed_before_taking_an_exam(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    assert await passed_flags(s) == {"check_in": False, "final": False}


async def test_a_check_in_pass_means_the_final_is_already_passed(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    await pass_exam(s, "check_in")
    assert await passed_flags(s) == {"check_in": True, "final": True}
    # Only a hint: the pass rule and the attempt policy do not change.
    items = (await s.get("/trainer/assessments")).json()["items"]
    assert [i["state"] for i in items] == ["passed", "available"]
    assert (await s.post("/trainer/assessments/final/attempts")).status_code == 201


async def test_without_the_exemption_the_check_in_does_not_cover_the_final(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams(exemption={"enabled": False})})
    s = await env["student"]()
    await pass_exam(s, "check_in")
    assert await passed_flags(s) == {"check_in": True, "final": False}


async def test_a_final_pass_is_already_passed(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    await pass_exam(s, "final")
    assert (await passed_flags(s))["final"] is True


async def test_a_failed_attempt_is_not_a_pass(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    a = (await start(s))["attempt_id"]
    await put(s, a, "wrong")
    await s.post(f"/trainer/attempts/{a}/submit")
    assert await passed_flags(s) == {"check_in": False, "final": False}


async def test_a_pass_belongs_to_its_student(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    await pass_exam(await env["student"]("student-1"), "check_in")
    other = await env["student"]("student-2")
    assert await passed_flags(other) == {"check_in": False, "final": False}


async def test_an_editor_in_preview_is_never_already_passed(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    ed = await env["editor"]()
    await pass_exam(ed, "check_in")
    assert await passed_flags(ed) == {"check_in": False, "final": False}
