"""Operator reports for the final-exam gate (read-only).

    python -m app.scripts.closing restricted --course <moodle course id>
    python -m app.scripts.closing check-service-account --tenant <tenant id> \\
        --uid <moodle user id> --course <moodle course id>

`restricted` lists the SCORM activities the gate would require that carry an access
restriction in Moodle (group, grouping, date, completion, ...). The gate counts
them as required, because the service account sees everything and cannot tell
what a restriction means for one student. One that never opens for some
students (typically a group or grouping restriction) would keep the final exam
closed for them: this report is what lets a person decide what to do about it.
Nothing is changed. Activity names are shown, so keep the output to operators.

`check-service-account` says, with the configured `MOODLE_SERVICE_TOKEN`,
whether `core_completion_get_activities_completion_status` returns data for one
user in one course, and which activities are left out: Moodle drops those the
**service account** cannot see. It reads the tenant's published map to say what
that means for the gate. It writes nothing, here or in Moodle, and prints
neither the token, the user id, a name nor Moodle's error texts, so the output
can be shared with whoever builds the completion provider. Exit status 0:
completion was read; 1: it was not; 2: no such tenant.
"""

import argparse
import asyncio
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from typing import Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.closing.gate import (
    REQUIRED_MODNAMES,
    required_scorm_cmids,
    restriction_kinds,
)
from app.core.config import get_settings
from app.core.database import create_engine
from app.models import Tenant
from app.moodle.cache import CachedMoodleClient
from app.moodle.client import HttpMoodleClient
from app.moodle.exceptions import MoodleError
from app.moodle.protocols import CourseContentsProvider
from app.moodle.schemas import MoodleCompletionStatuses, MoodleModule, MoodleSection
from app.repositories.sqlalchemy.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from app.schemas.map_settings import MapSettings
from app.services.course_map_service import is_bubble_candidate, is_hidden
from app.tenancy.scope import SYSTEM, Scope, bind_scope

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


class ServiceAccountReads(Protocol):
    """The two Moodle reads the check makes."""

    async def get_course_contents(self, course_id: int) -> list[MoodleSection]: ...

    async def get_activities_completion_status(
        self, course_id: int, moodle_user_id: int
    ) -> MoodleCompletionStatuses: ...


_COMPLETION_HINTS = {
    "accessexception": "the function is not in the token's web service, or the "
    "account may not use it",
    "requiredcapability": "the account's role lacks a capability in the course "
    "(reading others' progress needs report/progress:view)",
    "nopermissions": "the account's role lacks a capability in the course "
    "(reading others' progress needs report/progress:view)",
    "accessdenied": "the course uses separate groups and the account does not "
    "share one with this user",
    "invaliduser": "there is no such user",
    "invalidrecord": "there is no such user or course",
    "requireloginerror": "the account cannot enter the course",
}


def _code(value: str | None) -> str:
    """A Moodle code safe to print: a short identifier or nothing."""
    return value if value and re.fullmatch(r"[A-Za-z0-9_/:-]{1,64}", value) else "-"


def _failure(what: str, exc: MoodleError, out: Out) -> None:
    out(
        f"{what}: NOT readable ({type(exc).__name__}, errorcode={_code(exc.errorcode)})"
    )


def _why(section: MoodleSection, module: MoodleModule) -> str:
    """What the course contents, read by the same account, say about a module."""
    kinds = restriction_kinds(module.availability)
    # The visibility flags alone: what the account itself sees is told apart.
    flagged = not (section.visible and module.visible)
    return (
        f"hidden={'yes' if flagged else 'no'} "
        f"restricted_by={','.join(kinds) or '-'} "
        f"visible_to_service_account={'yes' if module.uservisible else 'no'}"
    )


async def _gate_bubbles(
    session: AsyncSession, course_id: int
) -> tuple[int, ...] | None:
    """The activity bubbles of the published map the gate takes its exam from
    (the first with the assessments on), or None when there is none."""
    rows, _ = await SqlAlchemyPublicationRepository(session).list_published(
        course_id, 100, 0
    )
    for row in rows:
        if MapSettings.model_validate(row.snapshot["settings"]).assessments.enabled:
            return tuple(
                b["activityId"]
                for b in row.snapshot["bubbles"]
                if b.get("activityId") is not None
            )
    return None


async def check_service_account(
    session: AsyncSession,
    moodle: ServiceAccountReads,
    tenant_id: uuid.UUID,
    course_id: int,
    moodle_user_id: int,
    *,
    moodle_base_url: str,
    out: Out = print,
) -> int:
    """Prints what the service account can read of one user's completion. The
    session must be scoped to the tenant. Returns the exit status."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        out("There is no tenant with this id")
        return 2
    out(f"tenant issuer: {tenant.issuer}")
    out(f"MOODLE_BASE_URL: {moodle_base_url}")
    if tenant.issuer.rstrip("/") != moodle_base_url.rstrip("/"):
        out(
            "WARNING: they differ. The service token belongs to MOODLE_BASE_URL; "
            "check that it is this tenant's site."
        )

    sections: list[MoodleSection] | None = None
    try:
        sections = await moodle.get_course_contents(course_id)
    except MoodleError as exc:
        _failure("course contents", exc, out)
    modules = {m.id: (s, m) for s in sections or [] for m in s.modules}
    tracked = {cmid for cmid, (_, m) in modules.items() if m.completion != 0}
    if sections is not None:
        out(
            f"course contents: {len(modules)} activities, {len(tracked)} tracked "
            "for completion"
        )

    try:
        answer = await moodle.get_activities_completion_status(
            course_id, moodle_user_id
        )
    except MoodleError as exc:
        _failure("completion", exc, out)
        hint = _COMPLETION_HINTS.get(exc.errorcode or "")
        if hint:
            out(f"  likely cause: {hint}")
        out("RESULT: completion was NOT read. Nothing was written.")
        return 1
    reported = {s.cmid: s for s in answer.statuses}
    codes = sorted({_code(w.warningcode) for w in answer.warnings})
    out(
        f"completion: readable, {len(reported)} activities reported, "
        f"{len(answer.warnings)} warning(s){' ' + ','.join(codes) if codes else ''}"
    )
    if sections is None:
        out("RESULT: completion was read, but not the course contents: no comparison.")
        out("Nothing was written.")
        return 1

    omitted = sorted(tracked - set(reported))
    out(
        f"omitted (tracked in the course contents, not in the completion answer): "
        f"{len(omitted)}"
    )
    for cmid in omitted:
        section, module = modules[cmid]
        out(f"  cmid={cmid} modname={module.modname} {_why(section, module)}")
    unknown = sorted(set(reported) - set(modules))
    out(f"reported but not in the course contents: {unknown or 'none'}")

    bubbles = await _gate_bubbles(session, course_id)
    if bubbles is None:
        out(
            "gate: no published map of this tenant and course has the assessments "
            "on: no required set to compare"
        )
        out("RESULT: completion was read. Nothing was written.")
        return 0
    required = sorted(required_scorm_cmids(sections, bubbles))
    blocked = [cmid for cmid in required if cmid not in reported]
    out(
        f"gate: {len(required)} required SCORM on the published map, "
        f"{len(required) - len(blocked)} reported, {len(blocked)} omitted"
    )
    for cmid in required:
        status = reported.get(cmid)
        out(
            f"  cmid={cmid} state={status.state} tracking={status.tracking}"
            if status
            else f"  cmid={cmid} OMITTED: it would count as not completed for "
            "every student"
        )
    # What the gate leaves out of the required set although it is a tracked
    # SCORM of the map: hidden from students, or invisible to the service
    # account, which the gate cannot tell apart from hidden.
    dropped = sorted(
        cmid
        for cmid in set(bubbles) & tracked
        if modules[cmid][1].modname in REQUIRED_MODNAMES and cmid not in required
    )
    out(f"tracked SCORM of the map that the gate does NOT require: {len(dropped)}")
    for cmid in dropped:
        section, module = modules[cmid]
        only_account = section.visible and module.visible and not module.uservisible
        flag = (
            "  <- no hidden flag, only the service account cannot see it: check "
            "whether students can"
            if only_account
            else ""
        )
        out(f"  cmid={cmid} {_why(section, module)}{flag}")
    absent = sorted(set(bubbles) - set(modules))
    out(
        f"map activities missing from the course contents (deleted, or not shown "
        f"to the service account; the gate would NOT require them): {absent or 'none'}"
    )
    out(
        f"RESULT: completion was read; {len(blocked)} required SCORM omitted, "
        f"{len(dropped)} tracked SCORM of the map not required, "
        f"{len(absent)} map activities missing. Nothing was written."
    )
    return 0


def _moodle_user_id(text: str) -> int:
    value = int(text)
    if not 0 < value <= 2147483647:
        raise argparse.ArgumentTypeError("not a Moodle user id")
    return value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.scripts.closing")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("restricted")
    r.add_argument("--course", type=int, required=True)
    c = sub.add_parser("check-service-account")
    c.add_argument("--tenant", type=uuid.UUID, required=True)
    c.add_argument("--uid", type=_moodle_user_id, required=True)
    c.add_argument("--course", type=int, required=True)
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
            live = HttpMoodleClient(
                http, settings.moodle_base_url, settings.moodle_service_token
            )
            if args.command == "check-service-account":
                bind_scope(session, Scope(args.tenant))
                return await check_service_account(
                    session,
                    live,
                    args.tenant,
                    args.course,
                    args.uid,
                    moodle_base_url=settings.moodle_base_url,
                )
            bind_scope(session, SYSTEM)
            # A single live read: no cache in a one-shot report.
            client = CachedMoodleClient(live, ttl_seconds=0, stale_max_seconds=0)
            await restricted_report(session, client, args.course)
    finally:
        await engine.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
