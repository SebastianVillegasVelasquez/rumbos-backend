"""Sends the outbox to Moodle and reconciles it. Run from cron (`sync-run`
every minute or so, `sync-reconcile` daily) through `app.scripts.passback`.

A transient answer retries later with exponential backoff (1 min doubling to
6 h, forever: an outage must not lose a result); a permanent refusal parks the
row as `failed_permanent` for an operator. Every step is appended to
`completion_audit`. Rows are taken with `FOR UPDATE SKIP LOCKED`, so two
workers never send the same statement.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompletionAudit, PassbackOutbox
from app.passback.port import MoodleResultPort, Outcome
from app.passback.statement import sign

MAX_BACKOFF = timedelta(hours=6)


def backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2 ** max(attempts - 1, 0)), MAX_BACKOFF)


def _audit(
    row: PassbackOutbox, event: str, at: datetime, **detail: Any
) -> CompletionAudit:
    return CompletionAudit(
        tenant_id=row.tenant_id,
        moodle_course_id=row.moodle_course_id,
        attempt_id=row.attempt_id,
        event=event,
        detail=detail,
        at=at,
    )


async def sync_run(
    session: AsyncSession,
    port: MoodleResultPort,
    key_pem: str,
    key_id: str,
    now: datetime,
    out: Callable[[str], None] = print,
    limit: int = 100,
) -> dict[str, int]:
    counts = {"sent": 0, "retry": 0, "failed": 0}
    rows = (
        await session.scalars(
            select(PassbackOutbox)
            .where(
                PassbackOutbox.status == "pending", PassbackOutbox.next_try_at <= now
            )
            .order_by(PassbackOutbox.next_try_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for row in rows:
        token = sign(row.statement, key_pem, key_id)
        result = await port.send(row.tenant_id, row.moodle_course_id, token)
        row.attempts += 1
        if result.outcome in (Outcome.ACCEPTED, Outcome.DUPLICATE):
            row.status, row.sent_at, row.last_error = "sent", now, None
            session.add(_audit(row, "sent", now, outcome=result.outcome.value))
            counts["sent"] += 1
        elif result.outcome is Outcome.TRANSIENT:
            row.next_try_at = now + backoff(row.attempts)
            row.last_error = result.detail[:500]
            session.add(_audit(row, "retry", now, error=result.detail[:200]))
            counts["retry"] += 1
        else:
            row.status, row.last_error = "failed_permanent", result.detail[:500]
            session.add(_audit(row, "failed_permanent", now, error=result.detail[:200]))
            counts["failed"] += 1
    await session.commit()
    out(f"Sent {counts['sent']}, to retry {counts['retry']}, failed {counts['failed']}")
    return counts


async def sync_reconcile(
    session: AsyncSession,
    port: MoodleResultPort,
    now: datetime,
    out: Callable[[str], None] = print,
) -> int:
    """Puts back in the queue every statement Moodle says it does not have
    (reported as sent, or refused, but not recorded there)."""
    rows = (
        await session.scalars(
            select(PassbackOutbox).where(
                PassbackOutbox.status.in_(("sent", "failed_permanent"))
            )
        )
    ).all()
    known: dict[tuple[Any, int], set[str]] = {}
    requeued = 0
    for row in rows:
        key = (row.tenant_id, row.moodle_course_id)
        if key not in known:
            known[key] = await port.known(*key)
        if str(row.attempt_id) in known[key]:
            continue
        was = row.status
        row.status, row.next_try_at = "pending", now
        session.add(_audit(row, "reconciled", now, was=was))
        requeued += 1
    await session.commit()
    out(f"Requeued {requeued} statement(s) Moodle does not have")
    return requeued
