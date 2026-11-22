"""Which settings apply to the caller's course, and the pieces every trainer
route shares: the clock, the per-user rate limit and who the caller is."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from app.assessment.service import Caller, Clock
from app.auth.dependencies import PrincipalDep, ScopedSession
from app.auth.rate_limit import FixedWindowLimiter
from app.closing.service import CourseContext
from app.core.config import Settings, get_settings
from app.enums import Role
from app.exceptions import TrainerNotFoundError, TrainerRateLimitedError
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.repositories.sqlalchemy.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from app.schemas.map_settings import MapSettings


def get_clock() -> Clock:
    """The server clock. Tests override this dependency with a fake one."""
    return lambda: datetime.now(UTC)


ClockDep = Annotated[Callable[[], datetime], Depends(get_clock)]


async def caller_of(principal: PrincipalDep) -> Caller:
    """The caller of a trainer route. The legacy anonymous principal has no
    course and no user, so it cannot sit an exam (404, as for a disabled
    feature)."""
    if principal.course_id is None or principal.sub is None:
        raise TrainerNotFoundError
    return Caller(
        principal.tenant_id,
        principal.course_id,
        principal.sub,
        preview=principal.role is Role.EDITOR,
    )


CallerDep = Annotated[Caller, Depends(caller_of)]


async def course_context(session: ScopedSession, caller: CallerDep) -> CourseContext:
    """The map the caller's exam settings come from (the first of the course's
    maps that enables the trainer or the assessments, in level order) with its
    activity bubbles. Students read what is published; an editor in preview
    reads the drafts, so a map can be tried before it is published. A course
    with none gets all defaults (off)."""
    found: list[CourseContext] = []
    if caller.preview:
        maps = SqlAlchemyCourseMapRepository(session)
        bubbles = SqlAlchemyBubbleRepository(session)
        for summary in await maps.list_for_course(caller.course_id):
            full = await maps.get_by_id(summary.id)
            if full:
                ids = tuple(
                    b.activity_id
                    for b in await bubbles.list_by_course_map(summary.id)
                    if b.activity_id is not None
                )
                found.append(CourseContext(full.settings, ids))
    else:
        rows, _ = await SqlAlchemyPublicationRepository(session).list_published(
            caller.course_id, 100, 0
        )
        found = [
            CourseContext(
                MapSettings.model_validate(r.snapshot["settings"]),
                tuple(
                    b["activityId"]
                    for b in r.snapshot["bubbles"]
                    if b.get("activityId") is not None
                ),
            )
            for r in rows
        ]
    for context in found:
        if context.settings.assessments.enabled:
            return context
    for context in found:
        if context.settings.trainer.enabled:
            return context
    return CourseContext(MapSettings(), ())


CourseContextDep = Annotated[CourseContext, Depends(course_context)]


async def course_settings(context: CourseContextDep) -> MapSettings:
    return context.settings


CourseSettingsDep = Annotated[MapSettings, Depends(course_settings)]


def rate_limited(
    request: Request,
    caller: CallerDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """429 when one user sends too many requests in a minute. Counts live in
    this process (see `app.auth.rate_limit`)."""
    limiter = getattr(request.app.state, "trainer_limiter", None)
    if limiter is None:
        limiter = FixedWindowLimiter(settings.trainer_rate_per_minute)
        request.app.state.trainer_limiter = limiter
    retry = limiter.hit(f"{caller.tenant_id}:{caller.sub}")
    if retry:
        raise TrainerRateLimitedError(retry)
