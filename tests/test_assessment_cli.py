"""The operator commands of the exam (sweep, purge, export, erase, stats)."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import (
    AssessmentAnswer,
    AssessmentAttempt,
    CompletionAudit,
    PassbackOutbox,
)
from app.models.tenant import LEGACY_TENANT_ID
from app.scripts import assessments as cli
from app.tenancy.scope import SYSTEM
from tests.sessions import new_session

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


async def seed(
    engine: AsyncEngine,
    *,
    sub: str,
    deadline: datetime,
    preview: bool = False,
    purpose: str = "check_in",
    answers: dict[str, Any] | None = None,
    status: str = "in_progress",
    created: datetime = NOW,
    course: int = 7,
) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    async with new_session(engine, SYSTEM) as s:
        s.add(
            AssessmentAttempt(
                id=attempt_id,
                tenant_id=LEGACY_TENANT_ID,
                moodle_course_id=course,
                sub=sub,
                purpose=purpose,
                ordinal=1,
                form_id="a1-form-a",
                content_version="v",
                status=status,
                preview=preview,
                seed="s",
                policy={
                    "pass_percent": 80,
                    "pass_operator": "gt",
                    "time_limit_min": 60,
                    "max_attempts": 1,
                },
                started_at=deadline - timedelta(hours=1),
                deadline_at=deadline,
                created_at=created,
                updated_at=created,
            )
        )
        await s.flush()
        for item_id, response in (answers or {}).items():
            s.add(
                AssessmentAnswer(
                    attempt_id=attempt_id,
                    item_id=item_id,
                    response=response,
                    saved_at=NOW,
                )
            )
        await s.commit()
    return attempt_id


async def row(engine: AsyncEngine, attempt_id: uuid.UUID) -> AssessmentAttempt | None:
    async with new_session(engine, SYSTEM) as s:
        return await s.get(AssessmentAttempt, attempt_id)


async def test_sweep_closes_only_attempts_past_their_deadline(
    engine: AsyncEngine,
) -> None:
    late = await seed(engine, sub="a", deadline=NOW - timedelta(minutes=1))
    inside = await seed(engine, sub="b", deadline=NOW + timedelta(minutes=1))
    lines: list[str] = []
    async with new_session(engine, SYSTEM) as s:
        assert await cli.sweep(s, NOW, lines.append) == 1
    closed, open_ = await row(engine, late), await row(engine, inside)
    assert closed and closed.status == "expired"
    assert closed.result and closed.result["percent"] == "0.00"
    assert open_ and open_.status == "in_progress"
    assert lines == ["Closed 1 expired attempt(s)"]


async def test_purge_preview_keeps_recent_and_real_attempts(
    engine: AsyncEngine,
) -> None:
    old = await seed(
        engine, sub="e1", deadline=NOW, preview=True, created=NOW - timedelta(hours=30)
    )
    recent = await seed(
        engine, sub="e2", deadline=NOW, preview=True, created=NOW - timedelta(hours=1)
    )
    real = await seed(engine, sub="s1", deadline=NOW, created=NOW - timedelta(hours=30))
    async with new_session(engine, SYSTEM) as s:
        assert await cli.purge_preview(s, NOW, 24, lambda _: None) == 1
    assert await row(engine, old) is None
    assert await row(engine, recent) and await row(engine, real)


async def test_export_prints_only_writing_and_speaking_of_real_closed_attempts(
    engine: AsyncEngine,
) -> None:
    free = {"values": {"q1": "Hello"}}
    await seed(
        engine,
        sub="s1",
        deadline=NOW,
        status="submitted",
        answers={"writing.tasks.01": free, "reading.a.01": {"option_id": "x"}},
    )
    await seed(
        engine, sub="s2", deadline=NOW, answers={"writing.tasks.01": free}
    )  # open
    await seed(
        engine,
        sub="e1",
        deadline=NOW,
        preview=True,
        status="submitted",
        answers={"writing.tasks.01": free},
    )
    lines: list[str] = []
    async with new_session(engine, SYSTEM) as s:
        assert (
            await cli.export_free_text(s, LEGACY_TENANT_ID, 7, None, lines.append) == 1
        )
    record = json.loads(lines[0])
    assert (record["sub"], record["item_id"], record["response"]) == (
        "s1",
        "writing.tasks.01",
        free,
    )


async def test_erase_removes_one_users_attempts_and_answers_only(
    engine: AsyncEngine,
) -> None:
    mine = await seed(
        engine, sub="gone", deadline=NOW, answers={"reading.a.01": {"option_id": "x"}}
    )
    other = await seed(
        engine, sub="stays", deadline=NOW, answers={"reading.a.01": {"option_id": "y"}}
    )
    async with new_session(engine, SYSTEM) as s:
        assert await cli.erase(s, LEGACY_TENANT_ID, "gone", lambda _: None) == 1
    assert await row(engine, mine) is None and await row(engine, other)
    async with new_session(engine, SYSTEM) as s:
        left = await s.scalar(select(func.count()).select_from(AssessmentAnswer))
    assert left == 1


async def test_erase_reaches_every_course_and_the_outbox_but_not_the_audit(
    engine: AsyncEngine,
) -> None:
    """The limits `docs/TESTING-closing-flow.md` states: every course of the
    tenant, previews too, and the outbox rows go with their attempts (a pending
    one is then never sent). The audit trail stays."""
    ids = [
        await seed(engine, sub="gone", deadline=NOW, status="submitted"),
        await seed(engine, sub="gone", deadline=NOW, status="submitted", course=8),
        await seed(engine, sub="gone", deadline=NOW, preview=True),
    ]
    async with new_session(engine, SYSTEM) as s:
        for attempt_id, state in zip(ids, ("pending", "sent"), strict=False):
            s.add(
                PassbackOutbox(
                    tenant_id=LEGACY_TENANT_ID,
                    moodle_course_id=7,
                    attempt_id=attempt_id,
                    statement={"passed": True},
                    status=state,
                    attempts=0,
                    next_try_at=NOW,
                )
            )
            s.add(
                CompletionAudit(
                    tenant_id=LEGACY_TENANT_ID,
                    moodle_course_id=7,
                    attempt_id=attempt_id,
                    event="enqueued",
                    detail={},
                    at=NOW,
                )
            )
        await s.commit()
    async with new_session(engine, SYSTEM) as s:
        assert await cli.erase(s, LEGACY_TENANT_ID, "gone", lambda _: None) == 3
    async with new_session(engine, SYSTEM) as s:
        assert await s.scalar(select(func.count()).select_from(AssessmentAttempt)) == 0
        assert await s.scalar(select(func.count()).select_from(PassbackOutbox)) == 0
        assert await s.scalar(select(func.count()).select_from(CompletionAudit)) == 2


async def test_stats_counts_by_purpose_status_and_band(engine: AsyncEngine) -> None:
    await seed(engine, sub="a", deadline=NOW)
    async with new_session(engine, SYSTEM) as s:
        counts = await cli.stats(s, lambda _: None)
    assert counts["check_in.in_progress.real"] == 1
