"""Which settings apply to the caller's course, and the pieces every trainer
route shares: the clock, the per-user rate limit and who the caller is."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from app.assessment.service import Caller, Clock
from app.auth.dependencies import PrincipalDep, ScopedSession
from app.auth.rate_limit import FixedWindowLimiter
from app.core.config import Settings, get_settings
from app.enums import Role
from app.exceptions import TrainerNotFoundError, TrainerRateLimitedError
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


async def course_settings(session: ScopedSession, caller: CallerDep) -> MapSettings:
    """The settings of the course's map that enables the trainer or the
    assessments: the first one in level order. Students read what is
    published; an editor in preview reads the drafts, so a map can be tried
    before it is published. A course with none gets all defaults (off)."""
    found: list[MapSettings] = []
    if caller.preview:
        maps = SqlAlchemyCourseMapRepository(session)
        for summary in await maps.list_for_course(caller.course_id):
            full = await maps.get_by_id(summary.id)
            if full:
                found.append(full.settings)
    else:
        rows, _ = await SqlAlchemyPublicationRepository(session).list_published(
            caller.course_id, 100, 0
        )
        found = [MapSettings.model_validate(r.snapshot["settings"]) for r in rows]
    for settings in found:
        if settings.assessments.enabled:
            return settings
    for settings in found:
        if settings.trainer.enabled:
            return settings
    return MapSettings()


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
