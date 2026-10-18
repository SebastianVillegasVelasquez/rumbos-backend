"""Publishes, as publication #1, every map that predates publishing.

    python -m app.scripts.publish_existing_maps           # dry run: only prints
    python -m app.scripts.publish_existing_maps --apply   # does it

Required once when deploying the draft/published release: until a map has a
publication, students (the `/published/...` API) cannot see it. A map is
included when it has at least one bubble and no publication. Safe to run
again: maps that already have a publication are never touched, so a second
run finds nothing to do.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable, Sequence
from typing import NamedTuple

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import create_engine
from app.exceptions import (
    CourseMapNotFoundError,
    NoChangesToPublishError,
    PublishConflictError,
    PublishInvalidError,
)
from app.models import Bubble, CourseMap, Publication
from app.repositories.sqlalchemy.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from app.tenancy.scope import SYSTEM, bind_scope

NOTE = "Publicación inicial (migración)"


class Candidate(NamedTuple):
    id: uuid.UUID
    title: str
    bubbles: int


async def find_candidates(session: AsyncSession) -> list[Candidate]:
    """Maps with at least one bubble and no publication, oldest first."""
    rows = await session.execute(
        select(CourseMap.id, CourseMap.title, func.count(Bubble.id))
        .join(Bubble, Bubble.course_map_id == CourseMap.id)
        .where(~exists().where(Publication.course_map_id == CourseMap.id))
        .group_by(CourseMap.id, CourseMap.title, CourseMap.created_at)
        .order_by(CourseMap.created_at, CourseMap.id)
    )
    candidates = [Candidate(*row) for row in rows]
    await session.rollback()  # reads only
    return candidates


async def run(
    session: AsyncSession,
    *,
    apply: bool,
    keep: int,
    out: Callable[[str], None] = print,
) -> int:
    """Returns how many maps were (or, in a dry run, would be) published."""
    candidates = await find_candidates(session)
    verb = "Publishing" if apply else "Would publish"
    done = 0
    for candidate in candidates:
        out(
            f"{verb} #1 of {candidate.id} {candidate.title!r} ({candidate.bubbles} bubbles)"
        )
        if not apply:
            done += 1
            continue
        try:
            await SqlAlchemyPublicationRepository(session).publish(
                candidate.id, None, NOTE, keep
            )
            done += 1
        except PublishInvalidError as exc:
            out(f"  SKIPPED, the map refers to things that are gone: {exc.problems}")
        except (CourseMapNotFoundError, NoChangesToPublishError, PublishConflictError):
            out("  SKIPPED, it changed while running; run the script again")
    suffix = "" if apply else " (dry run: pass --apply to do it)"
    out(f"{done} map(s) {'published' if apply else 'to publish'}{suffix}")
    return done


async def _main(apply: bool) -> int:
    settings = get_settings()
    engine = create_engine(settings.build_database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            # Housekeeping over every tenant's data, not one tenant's business.
            bind_scope(session, SYSTEM)
            await run(session, apply=apply, keep=settings.publications_keep)
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply", action="store_true", help="publish (default: dry run)"
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.apply))


if __name__ == "__main__":
    sys.exit(main())
