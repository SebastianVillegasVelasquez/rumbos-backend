"""Retention, reports and erasure of practice data."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import PracticeAnswer, PracticeReport, PracticeSession, SavedWord
from app.models.tenant import LEGACY_TENANT_ID
from app.scripts import practice as cli
from app.tenancy.scope import SYSTEM
from tests.sessions import new_session

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


async def seed(engine: AsyncEngine, sub: str, started: datetime) -> uuid.UUID:
    sid = uuid.uuid4()
    async with new_session(engine, SYSTEM) as s:
        s.add(
            PracticeSession(
                id=sid,
                tenant_id=LEGACY_TENANT_ID,
                moodle_course_id=7,
                sub=sub,
                activity_id="a",
                content_version="v",
                status="finished",
                seed="s",
                state={},
                started_at=started,
            )
        )
        await s.flush()
        s.add(
            PracticeAnswer(
                session_id=sid,
                step_id="x",
                attempt_id=uuid.uuid4(),
                outcome="correct",
                earned=1,
                total=1,
                answered_at=started,
            )
        )
        s.add(
            SavedWord(
                tenant_id=LEGACY_TENANT_ID,
                moodle_course_id=7,
                sub=sub,
                entry_id="v.1",
                box=2,
                due_at=NOW,
                reviews=1,
            )
        )
        s.add(
            PracticeReport(
                tenant_id=LEGACY_TENANT_ID,
                moodle_course_id=7,
                sub=sub,
                activity_id="a",
                step_id=None,
                content_version="v",
                text="odd",
            )
        )
        await s.commit()
    return sid


async def count(engine: AsyncEngine, model: type) -> int:
    async with new_session(engine, SYSTEM) as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def test_purge_drops_old_sessions_and_their_answers_but_keeps_words_and_reports(
    engine: AsyncEngine,
) -> None:
    await seed(engine, "old", NOW - timedelta(days=100))
    await seed(engine, "new", NOW - timedelta(days=5))
    async with new_session(engine, SYSTEM) as s:
        assert await cli.purge(s, NOW, 90, lambda _: None) == 1
    assert await count(engine, PracticeSession) == 1
    assert await count(engine, PracticeAnswer) == 1
    assert await count(engine, SavedWord) == 2
    assert await count(engine, PracticeReport) == 2


async def test_reports_lists_the_tenants_notes(engine: AsyncEngine) -> None:
    await seed(engine, "a", NOW)
    lines: list[str] = []
    async with new_session(engine, SYSTEM) as s:
        assert await cli.reports(s, LEGACY_TENANT_ID, 10, lines.append) == 1
    assert "odd" in lines[0]


async def test_erase_removes_one_users_practice_data(engine: AsyncEngine) -> None:
    await seed(engine, "gone", NOW)
    await seed(engine, "stays", NOW)
    async with new_session(engine, SYSTEM) as s:
        counts = await cli.erase(s, LEGACY_TENANT_ID, "gone", lambda _: None)
    assert counts == {"sessions": 1, "words": 1, "reports": 1}
    assert await count(engine, PracticeSession) == 1
    assert await count(engine, PracticeAnswer) == 1
