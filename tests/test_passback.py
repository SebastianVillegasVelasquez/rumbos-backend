# ruff: noqa: F811
"""Reporting exam results to Moodle: outbox, signing, retries, reconciliation."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import CompletionAudit, PassbackOutbox
from app.models.tenant import LEGACY_TENANT_ID
from app.passback.port import FakeMoodleResultPort
from app.passback.statement import verify
from app.passback.worker import backoff, sync_reconcile, sync_run
from app.tenancy.scope import SYSTEM
from tests.sessions import new_session
from tests.test_assessment_api import (  # noqa: F401
    FakeClock,
    clock,
    draft_content_is_served,
    env,
    exams,
    put,
    start,
)

_KEY = ec.generate_private_key(ec.SECP256R1())
PRIVATE = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC = (
    _KEY.public_key()
    .public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    .decode()
)
NOW = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)


async def finish(s: Any, kind: str, purpose: str = "check_in") -> dict[str, Any]:
    a = (await start(s, purpose))["attempt_id"]
    await put(s, a, kind)
    body: dict[str, Any] = (await s.post(f"/trainer/attempts/{a}/submit")).json()
    body["attempt_id"] = a
    return body


async def outbox(engine: AsyncEngine) -> list[PassbackOutbox]:
    async with new_session(engine, SYSTEM) as s:
        return list(
            await s.scalars(select(PassbackOutbox).order_by(PassbackOutbox.created_at))
        )


async def audit(engine: AsyncEngine) -> list[str]:
    async with new_session(engine, SYSTEM) as s:
        rows = await s.scalars(
            select(CompletionAudit).order_by(CompletionAudit.at, CompletionAudit.id)
        )
        return [r.event for r in rows]


async def run(
    engine: AsyncEngine, port: FakeMoodleResultPort, now: datetime = NOW
) -> dict[str, int]:
    async with new_session(engine, SYSTEM) as s:
        return await sync_run(s, port, PRIVATE, "k1", now, lambda _: None)


async def test_a_closed_real_attempt_is_queued_with_a_verifiable_statement(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    body = await finish(s, "right")
    assert body["sync"] == {"state": "pending"}
    (row,) = await outbox(env["engine"])
    assert (row.status, row.attempts) == ("pending", 0)
    port = FakeMoodleResultPort()
    assert await run(env["engine"], port) == {"sent": 1, "retry": 0, "failed": 0}
    token = port.received[0][2]
    assert jwt.get_unverified_header(token)["kid"] == "k1"
    claims = verify(token, PUBLIC, "rumbos-backend")
    assert claims["jti"] == body["attempt_id"] and claims["sub"] == "student-1"
    assert (claims["percent"], claims["passed"], claims["completes_course"]) == (
        "100.00",
        True,
        True,
    )
    assert (claims["purpose"], claims["course_id"], claims["pass_operator"]) == (
        "check_in",
        7,
        "gt",
    )
    result = (await s.get(f"/trainer/attempts/{body['attempt_id']}/result")).json()
    assert result["sync"] == {"state": "synced"}
    assert await audit(env["engine"]) == ["enqueued", "sent"]


async def test_preview_attempts_are_never_queued(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    ed = await env["editor"]()
    body = await finish(ed, "right")
    assert body["sync"] == {"state": "not_required"}
    assert await outbox(env["engine"]) == []


async def test_a_statement_carries_the_rule_that_was_in_force(
    env: dict[str, Any],
) -> None:
    await env["setup"](
        {
            "assessments": exams(
                exemption={"enabled": False}, passPercent=70, passOperator="gte"
            )
        }
    )
    s = await env["student"]()
    await finish(s, "right")
    (row,) = await outbox(env["engine"])
    assert row.statement["completes_course"] is False  # exemption off at that time
    assert (row.statement["pass_percent"], row.statement["pass_operator"]) == (
        70,
        "gte",
    )


async def test_a_failed_attempt_is_reported_but_does_not_complete(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    await finish(s, "wrong")
    (row,) = await outbox(env["engine"])
    assert (row.statement["passed"], row.statement["completes_course"]) == (
        False,
        False,
    )


async def test_moodle_down_then_up_loses_nothing(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    body = await finish(s, "right")
    port = FakeMoodleResultPort(mode="down")
    assert (await run(env["engine"], port))["retry"] == 1
    (row,) = await outbox(env["engine"])
    assert row.status == "pending" and row.attempts == 1 and row.last_error
    assert row.next_try_at == NOW + backoff(1)
    # Not due yet: nothing happens.
    assert await run(env["engine"], port, NOW + timedelta(seconds=30)) == {
        "sent": 0,
        "retry": 0,
        "failed": 0,
    }
    port.mode = "up"
    assert (await run(env["engine"], port, NOW + timedelta(hours=1)))["sent"] == 1
    assert (await s.get(f"/trainer/attempts/{body['attempt_id']}/result")).json()[
        "sync"
    ] == {"state": "synced"}
    assert await audit(env["engine"]) == ["enqueued", "retry", "sent"]


def test_backoff_doubles_and_is_capped() -> None:
    assert [backoff(n) for n in (1, 2, 3)] == [timedelta(minutes=m) for m in (1, 2, 4)]
    assert backoff(40) == timedelta(hours=6)


async def test_a_refusal_is_parked_for_an_operator(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    body = await finish(s, "right")
    port = FakeMoodleResultPort(mode="reject")
    assert (await run(env["engine"], port))["failed"] == 1
    assert (await s.get(f"/trainer/attempts/{body['attempt_id']}/result")).json()[
        "sync"
    ] == {"state": "failed"}
    assert await run(env["engine"], port, NOW + timedelta(days=1)) == {
        "sent": 0,
        "retry": 0,
        "failed": 0,
    }
    assert (await audit(env["engine"]))[-1] == "failed_permanent"


async def test_a_replayed_statement_changes_nothing(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    await finish(s, "right")
    port = FakeMoodleResultPort()
    await run(env["engine"], port)
    token = port.received[0][2]
    again = await port.send(LEGACY_TENANT_ID, 7, token)
    assert again.outcome.value == "duplicate"
    assert len(port.jtis[(LEGACY_TENANT_ID, 7)]) == 1


async def test_reconcile_requeues_what_moodle_lost_and_resend_is_idempotent(
    env: dict[str, Any],
) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    await finish(s, "right")
    port = FakeMoodleResultPort()
    await run(env["engine"], port)
    # Moodle still has it: nothing to do.
    async with new_session(env["engine"], SYSTEM) as session:
        assert await sync_reconcile(session, port, NOW, lambda _: None) == 0
    port.jtis.clear()  # Moodle lost it
    async with new_session(env["engine"], SYSTEM) as session:
        assert await sync_reconcile(session, port, NOW, lambda _: None) == 1
    assert (await outbox(env["engine"]))[0].status == "pending"
    assert (await run(env["engine"], port, NOW + timedelta(minutes=1)))["sent"] == 1
    assert len(port.jtis[(LEGACY_TENANT_ID, 7)]) == 1
    assert await audit(env["engine"]) == ["enqueued", "sent", "reconciled", "sent"]


async def test_one_statement_per_attempt(env: dict[str, Any]) -> None:
    await env["setup"]({"assessments": exams()})
    s = await env["student"]()
    body = await finish(s, "right")
    await s.post(f"/trainer/attempts/{body['attempt_id']}/submit")  # again
    assert len(await outbox(env["engine"])) == 1


async def test_the_statement_is_refused_with_the_wrong_key_or_issuer() -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(
        {"iss": "rumbos-backend", "aud": "moodle", "jti": str(uuid.uuid4())},
        other.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        algorithm="ES256",
    )
    with pytest.raises(jwt.PyJWTError):
        verify(token, PUBLIC, "rumbos-backend")
