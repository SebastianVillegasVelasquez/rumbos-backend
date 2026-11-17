"""Operator tools for the practice trainer (system scope).

    python -m app.scripts.practice purge [--days N]
    python -m app.scripts.practice reports --tenant <id> [--limit 100]
    python -m app.scripts.practice erase --tenant <id> --sub <user>

`purge` deletes practice sessions (and their answers) started more than
`--days` ago; the default is `TRAINER_RETENTION_DAYS` (90). Saved words and
reports are kept: words are the student's own list, reports are for the
content team. Run it daily from cron.

`reports` lists what students flagged for one tenant, newest first.

`erase` deletes every practice row (sessions, saved words, reports) of one user
of a tenant, for an erasure request. Exam attempts are separate (see
`app.scripts.assessments erase`).
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import create_engine
from app.models import PracticeReport, PracticeSession, SavedWord
from app.tenancy.scope import SYSTEM, bind_scope

Out = Callable[[str], None]


async def purge(
    session: AsyncSession, now: datetime, days: int, out: Out = print
) -> int:
    done = await session.execute(
        delete(PracticeSession).where(
            PracticeSession.started_at < now - timedelta(days=days)
        )
    )
    await session.commit()
    count = int(done.rowcount)  # type: ignore[attr-defined]
    out(f"Deleted {count} practice session(s) older than {days} days")
    return count


async def reports(
    session: AsyncSession, tenant: uuid.UUID, limit: int, out: Out = print
) -> int:
    rows = (
        await session.scalars(
            select(PracticeReport)
            .where(PracticeReport.tenant_id == tenant)
            .order_by(PracticeReport.created_at.desc())
            .limit(limit)
        )
    ).all()
    for r in rows:
        out(
            f"{r.created_at.isoformat()} course={r.moodle_course_id} "
            f"{r.activity_id}/{r.step_id or '-'} v={r.content_version} {r.text}"
        )
    return len(rows)


async def erase(
    session: AsyncSession, tenant: uuid.UUID, sub: str, out: Out = print
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, model in (
        ("sessions", PracticeSession),
        ("words", SavedWord),
        ("reports", PracticeReport),
    ):
        done = await session.execute(
            delete(model).where(model.tenant_id == tenant, model.sub == sub)
        )
        counts[label] = int(done.rowcount)  # type: ignore[attr-defined]
    await session.commit()
    out(f"Erased {counts}")
    return counts


async def _main(argv: Sequence[str] | None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.scripts.practice")
    sub = p.add_subparsers(dest="command", required=True)
    pu = sub.add_parser("purge")
    pu.add_argument("--days", type=int, default=None)
    re_ = sub.add_parser("reports")
    re_.add_argument("--tenant", type=uuid.UUID, required=True)
    re_.add_argument("--limit", type=int, default=100)
    er = sub.add_parser("erase")
    er.add_argument("--tenant", type=uuid.UUID, required=True)
    er.add_argument("--sub", required=True)
    args = p.parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings.build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            bind_scope(session, SYSTEM)
            if args.command == "purge":
                await purge(
                    session,
                    datetime.now(UTC),
                    args.days or settings.trainer_retention_days,
                )
            elif args.command == "reports":
                await reports(session, args.tenant, args.limit)
            else:
                await erase(session, args.tenant, args.sub)
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
