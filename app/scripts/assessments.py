"""Operator tools for the native exam. They run outside any tenant (system
scope), so every command names the tenant it works on where it matters.

    python -m app.scripts.assessments sweep
    python -m app.scripts.assessments purge-preview [--hours 24]
    python -m app.scripts.assessments list --tenant <id> --sub <user>
    python -m app.scripts.assessments export --tenant <id> --course <n> [--purpose final]
    python -m app.scripts.assessments erase --tenant <id> --sub <user>
    python -m app.scripts.assessments stats

`sweep` closes every attempt whose deadline has passed, scoring what was saved
(the API does the same lazily when the student comes back; run this from cron
so abandoned attempts do not stay open, e.g. every 10 minutes).

`purge-preview` deletes editors' preview attempts older than `--hours`.

`list` shows one user's attempts (no answers). `export` prints, one JSON object
per line, the Writing and Speaking responses of finished attempts of a course,
for the human review those parts may need; it is the only way free text leaves
the database. `erase` deletes every attempt and answer of one user of a tenant
(an erasure request): the exam results are gone for good, so run it only on a
written request and tell the teacher, because a passed exam may have been
reported to Moodle already. `stats` counts attempts by purpose, status and
band across the database.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.service import AssessmentService, Caller
from app.core.config import get_settings
from app.core.database import create_engine
from app.models import AssessmentAnswer, AssessmentAttempt
from app.repositories.sqlalchemy.assessment_repository import (
    SqlAlchemyAssessmentRepository,
)
from app.tenancy.scope import SYSTEM, bind_scope
from app.trainer.catalog import load_catalog

Out = Callable[[str], None]
PRODUCTIVE_PREFIXES = ("writing.", "speaking.")


async def sweep(
    session: AsyncSession,
    now: datetime,
    out: Out = print,
    limit: int = 500,
) -> int:
    catalog = load_catalog(Path(get_settings().trainer_content_dir))
    repo = SqlAlchemyAssessmentRepository(session)
    closed = 0
    for attempt in await repo.expired_open(now, limit):
        service = AssessmentService(
            repo,
            catalog,
            None,
            Caller(
                attempt.tenant_id,
                attempt.moodle_course_id,
                attempt.sub,
                attempt.preview,
            ),
            lambda: now,
            serve_drafts=True,
            audio_url=lambda *_: "",
            issuer=get_settings().passback_issuer,
        )
        await service.expire(attempt)
        closed += 1
    out(f"Closed {closed} expired attempt(s)")
    return closed


async def purge_preview(
    session: AsyncSession, now: datetime, hours: int, out: Out = print
) -> int:
    deleted = await SqlAlchemyAssessmentRepository(session).purge_preview(
        now - timedelta(hours=hours)
    )
    out(f"Deleted {deleted} preview attempt(s) older than {hours} h")
    return deleted


async def list_attempts(
    session: AsyncSession, tenant: uuid.UUID, sub: str, out: Out = print
) -> int:
    rows = (
        await session.scalars(
            select(AssessmentAttempt)
            .where(AssessmentAttempt.tenant_id == tenant, AssessmentAttempt.sub == sub)
            .order_by(AssessmentAttempt.started_at)
        )
    ).all()
    for r in rows:
        percent = r.result["percent"] if r.result else "-"
        out(
            f"{r.id} course={r.moodle_course_id} {r.purpose}#{r.ordinal} "
            f"{r.status}{' preview' if r.preview else ''} {percent} "
            f"started={r.started_at.isoformat()}"
        )
    return len(rows)


async def export_free_text(
    session: AsyncSession,
    tenant: uuid.UUID,
    course: int,
    purpose: str | None,
    out: Out = print,
) -> int:
    query = (
        select(AssessmentAttempt, AssessmentAnswer)
        .join(AssessmentAnswer, AssessmentAnswer.attempt_id == AssessmentAttempt.id)
        .where(
            AssessmentAttempt.tenant_id == tenant,
            AssessmentAttempt.moodle_course_id == course,
            AssessmentAttempt.preview.is_(False),
            AssessmentAttempt.status != "in_progress",
        )
        .order_by(AssessmentAttempt.started_at, AssessmentAnswer.item_id)
    )
    if purpose:
        query = query.where(AssessmentAttempt.purpose == purpose)
    count = 0
    for attempt, answer in (await session.execute(query)).all():
        if not answer.item_id.startswith(PRODUCTIVE_PREFIXES):
            continue
        out(
            json.dumps(
                {
                    "attempt_id": str(attempt.id),
                    "sub": attempt.sub,
                    "purpose": attempt.purpose,
                    "ordinal": attempt.ordinal,
                    "item_id": answer.item_id,
                    "response": answer.response,
                },
                ensure_ascii=False,
            )
        )
        count += 1
    return count


async def erase(
    session: AsyncSession, tenant: uuid.UUID, sub: str, out: Out = print
) -> int:
    ids = (
        await session.scalars(
            select(AssessmentAttempt.id).where(
                AssessmentAttempt.tenant_id == tenant, AssessmentAttempt.sub == sub
            )
        )
    ).all()
    if ids:
        await session.execute(
            delete(AssessmentAnswer).where(AssessmentAnswer.attempt_id.in_(ids))
        )
        await session.execute(
            delete(AssessmentAttempt).where(AssessmentAttempt.id.in_(ids))
        )
    await session.commit()
    out(f"Erased {len(ids)} attempt(s) of one user")
    return len(ids)


async def stats(session: AsyncSession, out: Out = print) -> Counter[str]:
    counts: Counter[str] = Counter()
    for a in (await session.scalars(select(AssessmentAttempt))).all():
        kind = "preview" if a.preview else "real"
        counts[f"{a.purpose}.{a.status}.{kind}"] += 1
        if a.result:
            counts[f"band.{a.result['band']}.{kind}"] += 1
    for key in sorted(counts):
        out(f"{key}: {counts[key]}")
    return counts


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.scripts.assessments")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("sweep")
    pp = sub.add_parser("purge-preview")
    pp.add_argument("--hours", type=int, default=24)
    ls = sub.add_parser("list")
    ls.add_argument("--tenant", type=uuid.UUID, required=True)
    ls.add_argument("--sub", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--tenant", type=uuid.UUID, required=True)
    ex.add_argument("--course", type=int, required=True)
    ex.add_argument("--purpose", choices=["check_in", "final"])
    er = sub.add_parser("erase")
    er.add_argument("--tenant", type=uuid.UUID, required=True)
    er.add_argument("--sub", required=True)
    sub.add_parser("stats")
    return p


async def _main(argv: Sequence[str] | None) -> int:
    args = _parser().parse_args(argv)
    engine = create_engine(get_settings().build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            bind_scope(session, SYSTEM)
            now = datetime.now(UTC)
            match args.command:
                case "sweep":
                    await sweep(session, now)
                case "purge-preview":
                    await purge_preview(session, now, args.hours)
                case "list":
                    await list_attempts(session, args.tenant, args.sub)
                case "export":
                    await export_free_text(
                        session, args.tenant, args.course, args.purpose
                    )
                case "erase":
                    await erase(session, args.tenant, args.sub)
                case "stats":
                    await stats(session)
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
