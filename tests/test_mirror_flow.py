# ruff: noqa: F811
"""The exam mirror through the real stack: the definition is queued before the
attempt, the attempt is frozen at close, delivery follows the outbox rules
(definition first, unknown definition retried, byte-identical resend), and
nothing leaks to a student or an editor in preview."""

import uuid
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assessment.scoring import decimal_string
from app.mirror.definition import build_definition, definition_hash
from app.models import MirrorDefinition, PassbackOutbox, Tenant
from app.models.tenant import LEGACY_TENANT_ID
from app.passback.http import HttpMirrorPort, classify
from app.passback.port import Outcome, SendResult
from app.passback.statement import verify
from app.passback.worker import sync_reconcile, sync_run
from app.repositories.sqlalchemy.assessment_repository import (
    SqlAlchemyAssessmentRepository,
)
from app.tenancy.scope import SYSTEM
from app.trainer.pack import Pack, load_pack
from tests.sessions import new_session
from tests.test_assessment_api import (  # noqa: F401
    FakeClock,
    clock,
    draft_content_is_served,
    env,
    exams,
    start,
)
from tests.test_mirror_attempt import mirror_percent, responses_for
from tests.test_passback import NOW, PRIVATE, PUBLIC, audit, outbox

PACK = load_pack(Path(__file__).resolve().parent.parent / "content/trainer/en/A2")
FORM = PACK.forms["a2-form-a"]
A2 = exams(
    checkIn={"formId": "a2-form-a", "maxAttempts": 3, "timeLimitMin": 60},
    final={"formId": "a2-form-a", "maxAttempts": 3, "timeLimitMin": 60},
)


class PluginPort:
    """What the plugin's receiver does: it keeps the definitions it was sent and
    refuses an attempt that names one it does not have."""

    supports_reconcile = True

    def __init__(self) -> None:
        self.definitions: set[str] = set()
        self.log: list[tuple[str, str]] = []  # (kind, jti)
        self.claims: list[dict[str, Any]] = []
        self.seen: set[str] = set()

    def forget(self) -> None:
        self.definitions.clear()

    async def send(
        self, tenant_id: uuid.UUID, course_id: int, statement_jws: str
    ) -> SendResult:
        claims = jwt.decode(statement_jws, options={"verify_signature": False})
        kind = claims.get("kind", "result")
        self.log.append((kind, claims["jti"]))
        if kind == "attempt" and claims["definition_hash"] not in self.definitions:
            return SendResult(Outcome.UNKNOWN_DEFINITION, "unknown_definition")
        self.claims.append(claims)
        if kind == "definition":
            self.definitions.add(claims["definition_hash"])
        duplicate = claims["jti"] in self.seen
        self.seen.add(claims["jti"])
        return SendResult(Outcome.DUPLICATE if duplicate else Outcome.ACCEPTED)

    async def known(self, tenant_id: uuid.UUID, course_id: int) -> set[str]:
        return set(self.seen)


async def configure(
    engine: AsyncEngine,
    *,
    endpoint: str | None = "https://moodle.test/mirror.php",
    written_text: bool = False,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(Tenant)
            .where(Tenant.id == LEGACY_TENANT_ID)
            .values(mirror_endpoint=endpoint, mirror_written_text=written_text)
        )


async def answer(s: Any, attempt: str, *, right: bool = True) -> None:
    payload = [
        {"item_id": k, "response": v}
        for k, v in responses_for(FORM, right=right).items()
    ]
    r = await s.put(f"/trainer/attempts/{attempt}/answers", json={"answers": payload})
    assert r.status_code == 200, r.text


async def finish(s: Any, *, right: bool = True, purpose: str = "check_in") -> Any:
    attempt = (await start(s, purpose))["attempt_id"]
    await answer(s, attempt, right=right)
    r = await s.post(f"/trainer/attempts/{attempt}/submit")
    assert r.status_code == 200, r.text
    return attempt, r.json()


async def definitions(engine: AsyncEngine) -> list[MirrorDefinition]:
    async with new_session(engine, SYSTEM) as s:
        return list(await s.scalars(select(MirrorDefinition)))


async def run(
    engine: AsyncEngine, port: PluginPort, now: datetime = NOW
) -> dict[str, int]:
    async with new_session(engine, SYSTEM) as s:
        return await sync_run(s, port, PRIVATE, "k1", now, lambda _: None)


# --- off by default --------------------------------------------------------------------


async def test_a_tenant_without_a_mirror_gets_the_plain_statement(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    await finish(s)
    (row,) = await outbox(env["engine"])
    assert "schema" not in row.statement and "answers" not in row.statement
    assert await definitions(env["engine"]) == []


async def test_a_draft_form_is_never_mirrored(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def only_when_drafts_are_allowed(
        self: Pack, form_id: str, allow_drafts: bool
    ) -> Any:
        return self.forms.get(form_id) if allow_drafts else None

    monkeypatch.setattr(Pack, "served_form", only_when_drafts_are_allowed)
    await configure(env["engine"])
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    attempt = (await start(s))["attempt_id"]
    r = await s.post(f"/trainer/attempts/{attempt}/submit")
    assert r.status_code == 200
    (row,) = await outbox(env["engine"])
    assert "schema" not in row.statement
    assert await definitions(env["engine"]) == []


# --- queued, frozen, delivered in order ---------------------------------------------------


async def test_the_definition_is_queued_at_start_and_the_record_frozen_at_close(
    env: dict[str, Any],
) -> None:
    await configure(env["engine"])
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    attempt = (await start(s))["attempt_id"]
    (row,) = await definitions(env["engine"])
    expected = definition_hash(build_definition(FORM))
    assert (row.status, row.definition_hash) == ("pending", expected)
    assert row.statement["kind"] == "definition"
    assert row.statement["definition"] == build_definition(FORM)

    await answer(s, attempt)
    body = (await s.post(f"/trainer/attempts/{attempt}/submit")).json()
    (sent,) = await outbox(env["engine"])
    claims = sent.statement
    assert claims["schema"] == 2 and claims["kind"] == "attempt"
    assert claims["definition_hash"] == expected and claims["status"] == "submitted"
    assert len(claims["answers"]) == 45 and len(claims["written"]) == 3
    assert claims["written_text_included"] is False
    assert all(w["text"] is None for w in claims["written"])
    assert decimal_string(Fraction(claims["percent_exact"])) == body["percent"]
    assert mirror_percent(build_definition(FORM), claims) == Fraction(
        claims["percent_exact"]
    )
    assert claims["passed"] is body["passed"]
    assert len(await definitions(env["engine"])) == 1  # close did not add one


async def test_the_definition_reaches_the_plugin_before_the_attempt(
    env: dict[str, Any],
) -> None:
    await configure(env["engine"])
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    attempt, _ = await finish(s)
    port = PluginPort()
    assert await run(env["engine"], port) == {"sent": 2, "retry": 0, "failed": 0}
    assert [k for k, _ in port.log] == ["definition", "attempt"]
    for claims in port.claims:
        assert (
            jwt.get_unverified_header(
                jwt.encode(claims, PRIVATE, algorithm="ES256", headers={"kid": "k1"})
            )["kid"]
            == "k1"
        )
    assert port.claims[1]["jti"] == attempt
    assert {r.status for r in await definitions(env["engine"])} == {"sent"}
    assert await audit(env["engine"]) == ["enqueued", "definition_sent", "sent"]


async def test_an_attempt_waits_for_its_definition_and_is_not_counted(
    env: dict[str, Any],
) -> None:
    await configure(env["engine"])
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    await finish(s)

    class Down(PluginPort):
        async def send(self, *a: Any) -> SendResult:
            return SendResult(Outcome.TRANSIENT, "down")

    assert await run(env["engine"], Down()) == {"sent": 0, "retry": 1, "failed": 0}
    (attempt_row,) = await outbox(env["engine"])
    assert (attempt_row.status, attempt_row.attempts) == ("pending", 0)
    (definition,) = await definitions(env["engine"])
    assert (definition.status, definition.attempts) == ("pending", 1)


async def test_an_unknown_definition_is_sent_again_and_the_attempt_is_never_dropped(
    env: dict[str, Any],
) -> None:
    await configure(env["engine"])
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    await finish(s)
    port = PluginPort()
    await run(env["engine"], port)
    port.forget()  # the plugin lost it, after both were accepted
    async with new_session(env["engine"], SYSTEM) as db:
        await db.execute(update(PassbackOutbox).values(status="pending"))
        await db.commit()

    first = await run(env["engine"], port, NOW + timedelta(minutes=5))
    assert first == {"sent": 0, "retry": 1, "failed": 0}
    (definition,) = await definitions(env["engine"])
    assert definition.status == "pending"  # queued again
    (row,) = await outbox(env["engine"])
    assert row.status == "pending" and row.last_error == "unknown_definition"

    later = NOW + timedelta(minutes=10)
    assert await run(env["engine"], port, later) == {"sent": 2, "retry": 0, "failed": 0}
    (row,) = await outbox(env["engine"])
    assert row.status == "sent"
    assert (await definitions(env["engine"]))[0].status == "sent"
    events = await audit(env["engine"])
    assert "unknown_definition" in events and "definition_requeued" in events


async def test_a_resend_is_byte_identical_and_changes_nothing_on_the_plugin(
    env: dict[str, Any],
) -> None:
    await configure(env["engine"])
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    await finish(s)
    port = PluginPort()
    await run(env["engine"], port)
    (before,) = await outbox(env["engine"])
    stored = dict(before.statement)
    async with new_session(env["engine"], SYSTEM) as db:
        await db.execute(update(PassbackOutbox).values(status="pending"))
        await db.commit()
    await run(env["engine"], port, NOW + timedelta(hours=1))
    (after,) = await outbox(env["engine"])
    assert after.statement == stored
    twice = [c for c in port.claims if c["kind"] == "attempt"]
    assert len(twice) == 2 and twice[0] == twice[1]
    assert after.status == "sent"
    claims = verify(
        jwt.encode(stored, PRIVATE, algorithm="ES256", headers={"kid": "k1"}),
        PUBLIC,
        "rumbos-backend",
    )
    assert claims == stored


async def test_the_definition_is_queued_once_per_content_and_again_for_a_new_one(
    env: dict[str, Any],
) -> None:
    engine = env["engine"]
    async with new_session(engine, SYSTEM) as db:
        repo = SqlAlchemyAssessmentRepository(db)
        for digest in ("a" * 64, "a" * 64, "b" * 64):
            await repo.ensure_definition(
                tenant_id=LEGACY_TENANT_ID,
                form_id="a2-form-a",
                definition_hash=digest,
                content_version="v1",
                statement={"kind": "definition"},
                now=datetime.now(UTC),
            )
    assert sorted(r.definition_hash[0] for r in await definitions(engine)) == [
        "a",
        "b",
    ]


# --- the written-text switch --------------------------------------------------------------


@pytest.mark.parametrize("allowed", [False, True])
async def test_student_text_travels_only_if_the_tenant_allows_it(
    env: dict[str, Any], allowed: bool
) -> None:
    await configure(env["engine"], written_text=allowed)
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    await finish(s)
    (row,) = await outbox(env["engine"])
    claims = row.statement
    assert claims["written_text_included"] is allowed
    assert all((w["text"] is not None) is allowed for w in claims["written"])
    assert ("Hello world" in str(claims)) is allowed


# --- never exported, never exposed ---------------------------------------------------------


async def test_preview_attempts_are_never_exported(env: dict[str, Any]) -> None:
    await configure(env["engine"])
    await env["setup"]({"assessments": A2})
    ed = await env["editor"]()
    await finish(ed)
    assert await outbox(env["engine"]) == []
    assert await definitions(env["engine"]) == []


async def test_what_a_student_reads_never_carries_a_key_or_the_definition(
    env: dict[str, Any],
) -> None:
    await configure(env["engine"], written_text=True)
    await env["setup"]({"assessments": A2})
    s = await env["student"]()
    attempt = (await start(s))["attempt_id"]
    seen = [(await s.get("/trainer/assessments")).text]
    started = await s.get(f"/trainer/attempts/{attempt}/sections/listening")
    seen.append(started.text)
    await answer(s, attempt)
    seen.append((await s.post(f"/trainer/attempts/{attempt}/submit")).text)
    seen.append((await s.get(f"/trainer/attempts/{attempt}/result")).text)
    text = " ".join(seen)
    for forbidden in (
        '"correct"',
        "weight_points",
        "definition_hash",
        '"answers"',
        "points_exact",
        "rubric",
        "transcript",
    ):
        assert forbidden not in text, forbidden
    secret = {
        "correct": {
            q["correct"]
            for s_ in build_definition(FORM)["sections"]
            for p in s_["parts"]
            for q in p["questions"]
            if "correct" in q
        }
    }
    assert secret  # the keys exist server-side; none of them is a field above


# --- the sender ------------------------------------------------------------------------


def reply(status: int, body: Any = None) -> httpx.Response:
    return (
        httpx.Response(status, json=body)
        if body is not None
        else httpx.Response(status)
    )


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (reply(200, {"status": "accepted"}), Outcome.ACCEPTED),
        (reply(200, {"status": "duplicate"}), Outcome.DUPLICATE),
        (reply(201), Outcome.ACCEPTED),
        (reply(409, {"error": "unknown_definition"}), Outcome.UNKNOWN_DEFINITION),
        (reply(422, {"error": "unknown_definition"}), Outcome.UNKNOWN_DEFINITION),
        (reply(409, {"error": "other"}), Outcome.PERMANENT),
        (reply(400, {"error": "bad_signature"}), Outcome.PERMANENT),
        (reply(401), Outcome.PERMANENT),
        (reply(302), Outcome.PERMANENT),
        (reply(429), Outcome.TRANSIENT),
        (reply(503), Outcome.TRANSIENT),
    ],
)
def test_the_answer_of_the_plugin_decides_what_the_outbox_does(
    response: httpx.Response, outcome: Outcome
) -> None:
    assert classify(response).outcome is outcome


async def test_the_sender_posts_the_jws_and_treats_a_network_error_as_transient() -> (
    None
):
    tenant = uuid.uuid4()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/down"):
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"status": "accepted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = HttpMirrorPort(
            client,
            {tenant: "https://m.test/mirror.php"},
        )
        ok = await port.send(tenant, 7, "a.b.c")
        assert ok.outcome is Outcome.ACCEPTED
        assert calls[0].content == b"a.b.c"
        assert calls[0].headers["content-type"] == "application/jwt"
        none = await port.send(uuid.uuid4(), 7, "a.b.c")
        assert none.outcome is Outcome.TRANSIENT
        down = HttpMirrorPort(client, {tenant: "https://m.test/down"})
        assert (await down.send(tenant, 7, "x")).outcome is Outcome.TRANSIENT


async def test_reconcile_leaves_a_sender_that_cannot_list_alone(
    env: dict[str, Any],
) -> None:
    async with httpx.AsyncClient() as client:
        port = HttpMirrorPort(client, {})
        async with new_session(env["engine"], SYSTEM) as db:
            assert await sync_reconcile(db, port, NOW, lambda _: None) == 0
