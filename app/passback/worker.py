"""Sends the outbox to Moodle and reconciles it. Run from cron (`sync-run`
every minute or so, `sync-reconcile` daily) through `app.scripts.passback`.

A transient answer retries later with exponential backoff (1 min doubling to
6 h, forever: an outage must not lose a result); a permanent refusal parks the
row as `failed_permanent` for an operator. Every step is appended to
`completion_audit`. Rows are taken with `FOR UPDATE SKIP LOCKED`, so two
workers never send the same statement.

Two kinds of row are sent, definitions first. An exam definition
(`mirror_definitions`) must reach the plugin before an attempt that names it:
an attempt whose definition is not `sent` yet simply waits (it is not counted
as a try), and one the plugin answers `unknown_definition` to puts its
definition back in the queue and is retried: it is never dropped.
"""

import uuid
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompletionAudit, MirrorDefinition, PassbackOutbox
from app.passback.port import MoodleResultPort, Outcome
from app.passback.statement import sign

MAX_BACKOFF = timedelta(hours=6)


def backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2 ** max(attempts - 1, 0)), MAX_BACKOFF)


def _audit(
    row: PassbackOutbox | MirrorDefinition,
    event: str,
    at: datetime,
    **detail: Any,
) -> CompletionAudit:
    return CompletionAudit(
        tenant_id=row.tenant_id,
        moodle_course_id=getattr(row, "moodle_course_id", 0),
        # A definition has no attempt: its own row id stands in the trail.
        attempt_id=getattr(row, "attempt_id", None) or row.id,
        event=event,
        detail=detail,
        at=at,
    )


async def _send_definitions(
    session: AsyncSession,
    port: MoodleResultPort,
    key_pem: str,
    key_id: str,
    now: datetime,
    tenants: Collection[uuid.UUID] | None,
    limit: int,
    counts: dict[str, int],
) -> None:
    query = (
        select(MirrorDefinition)
        .where(
            MirrorDefinition.status == "pending", MirrorDefinition.next_try_at <= now
        )
        .order_by(MirrorDefinition.next_try_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if tenants is not None:
        query = query.where(MirrorDefinition.tenant_id.in_(tenants))
    for row in (await session.scalars(query)).all():
        result = await port.send(row.tenant_id, 0, sign(row.statement, key_pem, key_id))
        row.attempts += 1
        if result.outcome in (Outcome.ACCEPTED, Outcome.DUPLICATE):
            row.status, row.sent_at, row.last_error = "sent", now, None
            session.add(_audit(row, "definition_sent", now, form=row.form_id))
            counts["sent"] += 1
        elif result.outcome is Outcome.PERMANENT:
            row.status, row.last_error = "failed_permanent", result.detail[:500]
            session.add(
                _audit(row, "definition_failed", now, error=result.detail[:200])
            )
            counts["failed"] += 1
        else:  # transient, or an odd answer to a definition: try again later
            row.next_try_at = now + backoff(row.attempts)
            row.last_error = result.detail[:500]
            session.add(_audit(row, "definition_retry", now, error=result.detail[:200]))
            counts["retry"] += 1


async def _definition_for(
    session: AsyncSession, row: PassbackOutbox
) -> MirrorDefinition | None:
    """The definition row a schema 2 attempt names, or None if it names none."""
    claims = row.statement
    if claims.get("schema") != 2:
        return None
    return await session.scalar(
        select(MirrorDefinition).where(
            MirrorDefinition.tenant_id == row.tenant_id,
            MirrorDefinition.form_id == claims["form_id"],
            MirrorDefinition.definition_hash == claims["definition_hash"],
        )
    )


async def sync_run(
    session: AsyncSession,
    port: MoodleResultPort,
    key_pem: str,
    key_id: str,
    now: datetime,
    out: Callable[[str], None] = print,
    limit: int = 100,
    tenants: Collection[uuid.UUID] | None = None,
) -> dict[str, int]:
    """Sends what is due. `tenants` narrows the run to those tenants (the real
    sender passes the ones that have an endpoint); None is every tenant."""
    counts = {"sent": 0, "retry": 0, "failed": 0}
    await _send_definitions(session, port, key_pem, key_id, now, tenants, limit, counts)
    query = (
        select(PassbackOutbox)
        .where(PassbackOutbox.status == "pending", PassbackOutbox.next_try_at <= now)
        .order_by(PassbackOutbox.next_try_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if tenants is not None:
        query = query.where(PassbackOutbox.tenant_id.in_(tenants))
    waiting = 0
    for row in (await session.scalars(query)).all():
        definition = await _definition_for(session, row)
        if row.statement.get("schema") == 2 and (
            definition is None or definition.status != "sent"
        ):
            waiting += 1  # its definition goes first; not a try
            continue
        token = sign(row.statement, key_pem, key_id)
        result = await port.send(row.tenant_id, row.moodle_course_id, token)
        row.attempts += 1
        if result.outcome in (Outcome.ACCEPTED, Outcome.DUPLICATE):
            row.status, row.sent_at, row.last_error = "sent", now, None
            session.add(_audit(row, "sent", now, outcome=result.outcome.value))
            counts["sent"] += 1
        elif result.outcome is Outcome.UNKNOWN_DEFINITION:
            if definition is not None:
                # Send the definition again before the attempt is retried.
                definition.status, definition.next_try_at = "pending", now
                session.add(_audit(definition, "definition_requeued", now))
            row.next_try_at = now + backoff(row.attempts)
            row.last_error = result.detail[:500]
            session.add(_audit(row, "unknown_definition", now))
            counts["retry"] += 1
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
    extra = f", waiting for a definition {waiting}" if waiting else ""
    out(
        f"Sent {counts['sent']}, to retry {counts['retry']}, "
        f"failed {counts['failed']}{extra}"
    )
    return counts


async def sync_reconcile(
    session: AsyncSession,
    port: MoodleResultPort,
    now: datetime,
    out: Callable[[str], None] = print,
) -> int:
    """Puts back in the queue every statement Moodle says it does not have
    (reported as sent, or refused, but not recorded there). A receiver that
    cannot list what it holds (`supports_reconcile` false) is left alone."""
    if not port.supports_reconcile:
        out("This sender cannot list what the receiver holds: nothing requeued")
        return 0
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
