"""Operator report for the final-exam gate (read-only).

    python -m app.scripts.closing restricted --course <moodle course id>

Lists the SCORM activities the gate would require that carry an access
restriction in Moodle (group, grouping, date, completion, ...). The gate counts
them as required, because the service account sees everything and cannot tell
what a restriction means for one student. One that never opens for some
students (typically a group or grouping restriction) would keep the final exam
closed for them: this report is what lets a person decide what to do about it.
Nothing is changed. Activity names are shown, so keep the output to operators.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.closing.gate import REQUIRED_MODNAMES, restriction_kinds
from app.core.config import get_settings
from app.core.database import create_engine
from app.moodle.cache import CachedMoodleClient
from app.moodle.client import HttpMoodleClient
from app.moodle.exceptions import MoodleError
from app.moodle.protocols import CourseContentsProvider
from app.repositories.sqlalchemy.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from app.services.course_map_service import is_bubble_candidate, is_hidden
from app.tenancy.scope import SYSTEM, bind_scope

Out = Callable[[str], None]


async def restricted_report(
    session: AsyncSession,
    contents: CourseContentsProvider,
    course_id: int,
    out: Out = print,
) -> int:
    """Prints the restricted required SCORM of every published map of the
    course; returns how many there are."""
    rows, _ = await SqlAlchemyPublicationRepository(session).list_published(
        course_id, 100, 0
    )
    try:
        sections = (await contents.fetch_course_contents(course_id)).sections
    except MoodleError as exc:
        out(f"Moodle could not be read ({type(exc).__name__}); nothing to report")
        return 0
    modules = {m.id: (s, m) for s in sections for m in s.modules}
    found = 0
    for row in rows:
        title = row.snapshot["title"]
        for bubble in row.snapshot["bubbles"]:
            entry = modules.get(bubble.get("activityId", -1))
            if entry is None:
                continue
            section, module = entry
            if (
                module.modname not in REQUIRED_MODNAMES
                or not is_bubble_candidate(module)
                or is_hidden(section, module)
                or module.completion == 0
            ):
                continue
            kinds = restriction_kinds(module.availability)
            if not kinds:
                continue
            found += 1
            flag = "  <- may never open for some students" if _by_group(kinds) else ""
            out(
                f"map={title!r} cmid={module.id} name={module.name!r} "
                f"restricted_by={','.join(kinds)}{flag}"
            )
    out(f"{found} restricted required SCORM activity(ies) in course {course_id}")
    return found


def _by_group(kinds: list[str]) -> bool:
    return "group" in kinds or "grouping" in kinds


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.scripts.closing")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("restricted")
    r.add_argument("--course", type=int, required=True)
    return p


async def _main(argv: Sequence[str] | None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings.build_database_url)
    try:
        async with (
            httpx.AsyncClient(timeout=settings.moodle_read_timeout) as http,
            AsyncSession(engine, expire_on_commit=False) as session,
        ):
            bind_scope(session, SYSTEM)
            # A single live read: no cache in a one-shot report.
            client = CachedMoodleClient(
                HttpMoodleClient(
                    http, settings.moodle_base_url, settings.moodle_service_token
                ),
                ttl_seconds=0,
                stale_max_seconds=0,
            )
            await restricted_report(session, client, args.course)
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
