"""Storage of practice sessions, answers, saved words and reports. Like the
exam repository, it runs on a scoped session, so only the caller's course is
visible; `sub` narrows to one user."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PracticeAnswer, PracticeReport, PracticeSession, SavedWord


@dataclass(frozen=True)
class SessionRecord:
    id: uuid.UUID
    activity_id: str
    content_version: str
    status: str
    seed: str
    state: Mapping[str, Any]
    started_at: datetime
    finished_at: datetime | None
    summary: Mapping[str, Any] | None


@dataclass(frozen=True)
class AnswerRecord:
    step_id: str
    attempt_id: uuid.UUID
    outcome: str
    earned: int
    total: int
    error_tag: str | None


@dataclass(frozen=True)
class WordRecord:
    entry_id: str
    box: int
    due_at: datetime
    reviews: int


def _session(row: PracticeSession) -> SessionRecord:
    return SessionRecord(
        row.id,
        row.activity_id,
        row.content_version,
        row.status,
        row.seed,
        row.state,
        row.started_at,
        row.finished_at,
        row.summary,
    )


def _word(row: SavedWord) -> WordRecord:
    return WordRecord(row.entry_id, row.box, row.due_at, row.reviews)


class SqlAlchemyPracticeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- sessions ----------------------------------------------------------------

    async def get_session(
        self, session_id: uuid.UUID
    ) -> tuple[SessionRecord, str] | None:
        row = await self._s.get(PracticeSession, session_id)
        found = (_session(row), row.sub) if row else None
        await self._s.rollback()
        return found

    async def open_session(self, sub: str, activity_id: str) -> SessionRecord | None:
        row = await self._s.scalar(
            select(PracticeSession).where(
                PracticeSession.sub == sub,
                PracticeSession.activity_id == activity_id,
                PracticeSession.status == "in_progress",
            )
        )
        found = _session(row) if row else None
        await self._s.rollback()
        return found

    async def create_session(
        self,
        *,
        course_id: int,
        sub: str,
        activity_id: str,
        content_version: str,
        seed: str,
        state: Mapping[str, Any],
        now: datetime,
    ) -> SessionRecord | None:
        """None when the user already has one open for the activity (a racing
        request got there first)."""
        try:
            row = PracticeSession(
                moodle_course_id=course_id,
                sub=sub,
                activity_id=activity_id,
                content_version=content_version,
                status="in_progress",
                seed=seed,
                state=dict(state),
                started_at=now,
            )
            self._s.add(row)
            await self._s.commit()
            return _session(row)
        except IntegrityError:
            await self._s.rollback()
            return None
        except BaseException:
            await self._s.rollback()
            raise

    async def set_state(self, session_id: uuid.UUID, state: Mapping[str, Any]) -> None:
        await self._s.execute(
            update(PracticeSession)
            .where(PracticeSession.id == session_id)
            .values(state=dict(state))
        )
        await self._s.commit()

    async def finish(
        self, session_id: uuid.UUID, now: datetime, summary: Mapping[str, Any]
    ) -> bool:
        done = await self._s.execute(
            update(PracticeSession)
            .where(
                PracticeSession.id == session_id,
                PracticeSession.status == "in_progress",
            )
            .values(status="finished", finished_at=now, summary=dict(summary))
        )
        await self._s.commit()
        return bool(done.rowcount)  # type: ignore[attr-defined]

    async def sessions_of(self, sub: str) -> list[SessionRecord]:
        rows = (
            await self._s.scalars(
                select(PracticeSession)
                .where(PracticeSession.sub == sub)
                .order_by(PracticeSession.started_at)
            )
        ).all()
        found = [_session(r) for r in rows]
        await self._s.rollback()
        return found

    # --- answers -----------------------------------------------------------------

    async def answers(self, session_id: uuid.UUID) -> list[AnswerRecord]:
        rows = (
            await self._s.scalars(
                select(PracticeAnswer)
                .where(PracticeAnswer.session_id == session_id)
                .order_by(PracticeAnswer.answered_at, PracticeAnswer.id)
            )
        ).all()
        found = [
            AnswerRecord(
                r.step_id, r.attempt_id, r.outcome, r.earned, r.total, r.error_tag
            )
            for r in rows
        ]
        await self._s.rollback()
        return found

    async def record_answer(
        self,
        session_id: uuid.UUID,
        record: AnswerRecord,
        now: datetime,
    ) -> bool:
        """False when this `attempt_id` was already recorded (a retry)."""
        try:
            self._s.add(
                PracticeAnswer(
                    session_id=session_id,
                    step_id=record.step_id,
                    attempt_id=record.attempt_id,
                    outcome=record.outcome,
                    earned=record.earned,
                    total=record.total,
                    error_tag=record.error_tag,
                    answered_at=now,
                )
            )
            await self._s.commit()
            return True
        except IntegrityError:
            await self._s.rollback()
            return False
        except BaseException:
            await self._s.rollback()
            raise

    # --- saved words -------------------------------------------------------------

    async def words(self, sub: str) -> list[WordRecord]:
        rows = (
            await self._s.scalars(
                select(SavedWord).where(SavedWord.sub == sub).order_by(SavedWord.due_at)
            )
        ).all()
        found = [_word(r) for r in rows]
        await self._s.rollback()
        return found

    async def save_word(
        self, course_id: int, sub: str, entry_id: str, now: datetime
    ) -> WordRecord:
        row = await self._s.scalar(
            select(SavedWord).where(
                SavedWord.sub == sub, SavedWord.entry_id == entry_id
            )
        )
        if row is None:
            row = SavedWord(
                moodle_course_id=course_id,
                sub=sub,
                entry_id=entry_id,
                box=1,
                due_at=now,
                reviews=0,
            )
            self._s.add(row)
            try:
                await self._s.commit()
            except IntegrityError:  # a racing save: it is already there
                await self._s.rollback()
                row = await self._s.scalar(
                    select(SavedWord).where(
                        SavedWord.sub == sub, SavedWord.entry_id == entry_id
                    )
                )
                assert row is not None
        found = _word(row)
        await self._s.rollback()
        return found

    async def review_word(
        self, sub: str, entry_id: str, box: int, due_at: datetime
    ) -> WordRecord | None:
        row = await self._s.scalar(
            select(SavedWord).where(
                SavedWord.sub == sub, SavedWord.entry_id == entry_id
            )
        )
        if row is None:
            await self._s.rollback()
            return None
        row.box, row.due_at, row.reviews = box, due_at, row.reviews + 1
        found = _word(row)
        await self._s.commit()
        return found

    async def forget_word(self, sub: str, entry_id: str) -> bool:
        done = await self._s.execute(
            delete(SavedWord).where(
                SavedWord.sub == sub, SavedWord.entry_id == entry_id
            )
        )
        await self._s.commit()
        return bool(done.rowcount)  # type: ignore[attr-defined]

    # --- reports -----------------------------------------------------------------

    async def add_report(
        self,
        course_id: int,
        sub: str,
        activity_id: str,
        step_id: str | None,
        content_version: str,
        text: str,
    ) -> uuid.UUID:
        row = PracticeReport(
            moodle_course_id=course_id,
            sub=sub,
            activity_id=activity_id,
            step_id=step_id,
            content_version=content_version,
            text=text,
            status="new",
        )
        self._s.add(row)
        await self._s.commit()
        return row.id

    async def reports(self, limit: int) -> list[dict[str, Any]]:
        rows = (
            await self._s.scalars(
                select(PracticeReport)
                .order_by(PracticeReport.created_at.desc())
                .limit(limit)
            )
        ).all()
        found = [
            {
                "id": r.id,
                "activity_id": r.activity_id,
                "step_id": r.step_id,
                "content_version": r.content_version,
                "text": r.text,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in rows
        ]
        await self._s.rollback()
        return found

    # --- erasure and retention -----------------------------------------------------

    async def erase_user(self, sub: str) -> dict[str, int]:
        """Everything practice-related of one user in the scoped course."""
        counts: dict[str, int] = {}
        for label, model in (
            ("sessions", PracticeSession),
            ("words", SavedWord),
            ("reports", PracticeReport),
        ):
            done = await self._s.execute(delete(model).where(model.sub == sub))
            counts[label] = int(done.rowcount)  # type: ignore[attr-defined]
        await self._s.commit()
        return counts

    async def purge_sessions(self, older_than: datetime) -> int:
        done = await self._s.execute(
            delete(PracticeSession).where(PracticeSession.started_at < older_than)
        )
        await self._s.commit()
        return int(done.rowcount)  # type: ignore[attr-defined]
