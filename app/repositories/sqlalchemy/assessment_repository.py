"""Storage of assessment attempts and answers.

The session carries the tenant and course scope (see `app.tenancy.scope`), so
every query here sees only the caller's course; `sub` narrows it to one user.
Writes commit; failures roll back.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from app.models import (
    AssessmentAnswer,
    AssessmentAttempt,
    CompletionAudit,
    MirrorDefinition,
    PassbackOutbox,
    Tenant,
)


@dataclass(frozen=True)
class MirrorConfig:
    """A tenant's mirror switch: where it goes, and whether student text may."""

    endpoint: str
    written_text: bool


@dataclass(frozen=True)
class AttemptRecord:
    id: uuid.UUID
    tenant_id: uuid.UUID
    moodle_course_id: int
    sub: str
    purpose: str
    ordinal: int
    form_id: str
    content_version: str
    status: str
    preview: bool
    seed: str
    policy: Mapping[str, Any]
    started_at: datetime
    deadline_at: datetime
    finished_at: datetime | None
    result: Mapping[str, Any] | None


def _record(row: AssessmentAttempt) -> AttemptRecord:
    return AttemptRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        moodle_course_id=row.moodle_course_id,
        sub=row.sub,
        purpose=row.purpose,
        ordinal=row.ordinal,
        form_id=row.form_id,
        content_version=row.content_version,
        status=row.status,
        preview=row.preview,
        seed=row.seed,
        policy=row.policy,
        started_at=row.started_at,
        deadline_at=row.deadline_at,
        finished_at=row.finished_at,
        result=row.result,
    )


class SqlAlchemyAssessmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, attempt_id: uuid.UUID) -> AttemptRecord | None:
        row = await self._session.get(AssessmentAttempt, attempt_id)
        record = _record(row) if row else None
        await self._session.rollback()
        return record

    async def list_for_user(self, sub: str, preview: bool) -> list[AttemptRecord]:
        rows = (
            await self._session.scalars(
                select(AssessmentAttempt)
                .where(
                    AssessmentAttempt.sub == sub, AssessmentAttempt.preview == preview
                )
                .order_by(AssessmentAttempt.purpose, AssessmentAttempt.ordinal)
            )
        ).all()
        records = [_record(r) for r in rows]
        await self._session.rollback()
        return records

    async def create(
        self,
        *,
        moodle_course_id: int,
        sub: str,
        purpose: str,
        form_id: str,
        content_version: str,
        seed: str,
        policy: Mapping[str, Any],
        started_at: datetime,
        deadline_at: datetime,
        preview: bool,
        max_attempts: int | None,
    ) -> AttemptRecord | None:
        """Starts the next attempt, or returns None when the user already has
        one in progress (including one a racing request just started) or, with
        `max_attempts`, has used them all. The numbering and the one-open rule
        are enforced by unique indexes, so two tabs cannot both win."""
        try:
            used = await self._session.scalar(
                select(func.count())
                .select_from(AssessmentAttempt)
                .where(
                    AssessmentAttempt.sub == sub,
                    AssessmentAttempt.purpose == purpose,
                    AssessmentAttempt.preview == preview,
                )
            )
            if max_attempts is not None and (used or 0) >= max_attempts:
                await self._session.rollback()
                return None
            row = AssessmentAttempt(
                moodle_course_id=moodle_course_id,
                sub=sub,
                purpose=purpose,
                ordinal=(used or 0) + 1,
                form_id=form_id,
                content_version=content_version,
                status="in_progress",
                preview=preview,
                seed=seed,
                policy=dict(policy),
                started_at=started_at,
                deadline_at=deadline_at,
            )
            self._session.add(row)
            await self._session.commit()
            return _record(row)
        except IntegrityError:
            await self._session.rollback()
            return None
        except BaseException:
            await self._session.rollback()
            raise

    async def open_attempt(
        self, sub: str, purpose: str, preview: bool
    ) -> AttemptRecord | None:
        row = await self._session.scalar(
            select(AssessmentAttempt).where(
                AssessmentAttempt.sub == sub,
                AssessmentAttempt.purpose == purpose,
                AssessmentAttempt.preview == preview,
                AssessmentAttempt.status == "in_progress",
            )
        )
        record = _record(row) if row else None
        await self._session.rollback()
        return record

    async def answers(self, attempt_id: uuid.UUID) -> dict[str, dict[str, Any]]:
        rows = (
            await self._session.execute(
                select(AssessmentAnswer.item_id, AssessmentAnswer.response).where(
                    AssessmentAnswer.attempt_id == attempt_id
                )
            )
        ).all()
        await self._session.rollback()
        return {item_id: dict(response) for item_id, response in rows}

    async def save_answers(
        self,
        attempt_id: uuid.UUID,
        answers: Mapping[str, Mapping[str, Any]],
        saved_at: datetime,
    ) -> bool:
        """Last write wins, per item. False when the attempt is no longer in
        progress (checked in the same transaction as the write)."""
        try:
            open_ = await self._session.scalar(
                select(AssessmentAttempt.id)
                .where(
                    AssessmentAttempt.id == attempt_id,
                    AssessmentAttempt.status == "in_progress",
                )
                .with_for_update()
            )
            if open_ is None:
                await self._session.rollback()
                return False
            for item_id, response in answers.items():
                stmt = pg_insert(AssessmentAnswer).values(
                    id=uuid.uuid4(),
                    attempt_id=attempt_id,
                    item_id=item_id,
                    response=dict(response),
                    saved_at=saved_at,
                    created_at=saved_at,
                    updated_at=saved_at,
                )
                await self._session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_assessment_answers_attempt_item",
                        set_={
                            "response": stmt.excluded.response,
                            "saved_at": saved_at,
                            "updated_at": saved_at,
                        },
                    )
                )
            await self._session.commit()
            return True
        except BaseException:
            await self._session.rollback()
            raise

    async def close(
        self,
        attempt_id: uuid.UUID,
        status: str,
        finished_at: datetime,
        result: Mapping[str, Any],
        statement: Mapping[str, Any] | None = None,
    ) -> bool:
        """Closes an attempt that is still in progress; False if someone else
        closed it first (the caller then reads the stored result). With a
        `statement` (a real attempt's claims for Moodle) it is put in the
        passback outbox in the same transaction, so the result and its report
        stand or fall together."""
        try:
            done = await self._session.execute(
                update(AssessmentAttempt)
                .where(
                    AssessmentAttempt.id == attempt_id,
                    AssessmentAttempt.status == "in_progress",
                )
                .values(status=status, finished_at=finished_at, result=dict(result))
            )
            closed = bool(done.rowcount)  # type: ignore[attr-defined]
            if closed and statement is not None:
                attempt = await self._session.get(AssessmentAttempt, attempt_id)
                assert attempt is not None
                self._session.add(
                    PassbackOutbox(
                        tenant_id=attempt.tenant_id,
                        moodle_course_id=attempt.moodle_course_id,
                        attempt_id=attempt_id,
                        statement=dict(statement),
                        status="pending",
                        attempts=0,
                        next_try_at=finished_at,
                    )
                )
                self._session.add(
                    CompletionAudit(
                        tenant_id=attempt.tenant_id,
                        moodle_course_id=attempt.moodle_course_id,
                        attempt_id=attempt_id,
                        event="enqueued",
                        detail={"passed": statement["passed"]},
                        at=finished_at,
                    )
                )
            await self._session.commit()
            return closed
        except BaseException:
            await self._session.rollback()
            raise

    async def mirror_config(self, tenant_id: uuid.UUID) -> MirrorConfig | None:
        """The tenant's mirror settings, or None while its mirror is off."""
        row = (
            await self._session.execute(
                select(Tenant.mirror_endpoint, Tenant.mirror_written_text).where(
                    Tenant.id == tenant_id
                )
            )
        ).first()
        await self._session.rollback()
        if row is None or not row[0]:
            return None
        return MirrorConfig(endpoint=row[0], written_text=bool(row[1]))

    async def ensure_definition(
        self,
        *,
        tenant_id: uuid.UUID,
        form_id: str,
        definition_hash: str,
        content_version: str,
        statement: Mapping[str, Any],
        now: datetime,
    ) -> None:
        """Queues the definition for the plugin, once: a form that did not
        change (same hash) is already there and stays as it is."""
        try:
            await self._session.execute(
                pg_insert(MirrorDefinition)
                .values(
                    id=uuid7(),
                    created_at=now,
                    updated_at=now,
                    tenant_id=tenant_id,
                    form_id=form_id,
                    definition_hash=definition_hash,
                    content_version=content_version,
                    statement=dict(statement),
                    status="pending",
                    attempts=0,
                    next_try_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "form_id", "definition_hash"]
                )
            )
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise

    async def sync_states(self, attempt_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """The outbox status of each attempt that has a row."""
        rows = (
            await self._session.execute(
                select(PassbackOutbox.attempt_id, PassbackOutbox.status).where(
                    PassbackOutbox.attempt_id.in_(attempt_ids)
                )
            )
        ).all()
        await self._session.rollback()
        return {attempt_id: status for attempt_id, status in rows}

    async def expired_open(self, now: datetime, limit: int) -> list[AttemptRecord]:
        rows = (
            await self._session.scalars(
                select(AssessmentAttempt)
                .where(
                    AssessmentAttempt.status == "in_progress",
                    AssessmentAttempt.deadline_at <= now,
                )
                .order_by(AssessmentAttempt.deadline_at)
                .limit(limit)
            )
        ).all()
        records = [_record(r) for r in rows]
        await self._session.rollback()
        return records

    async def purge_preview(self, older_than: datetime) -> int:
        try:
            done = await self._session.execute(
                delete(AssessmentAttempt).where(
                    AssessmentAttempt.preview.is_(True),
                    AssessmentAttempt.created_at < older_than,
                )
            )
            await self._session.commit()
            return int(done.rowcount)  # type: ignore[attr-defined]
        except BaseException:
            await self._session.rollback()
            raise
