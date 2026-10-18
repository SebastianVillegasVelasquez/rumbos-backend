"""The student-facing read API: published maps only.

Students (and editors) may read it, for their own course only: the session is
scoped to the token's tenant and course, so any other map is a 404. For the
legacy anonymous principal this is a consistent view, not access control.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import ScopedSession, moodle_source_route, student_route
from app.moodle.dependencies import get_contents_provider
from app.moodle.protocols import CourseContentsProvider
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.repositories.sqlalchemy.publication_repository import (
    SqlAlchemyPublicationRepository,
)
from app.schemas.published import PublishedMapList, PublishedMapRead
from app.schemas.resolved import ResolvedMap
from app.services.published_service import PublishedMapService
from app.services.resolution_service import MapResolutionService

router = APIRouter(
    prefix="/published/course-maps",
    tags=["published"],
    dependencies=[Depends(student_route)],
)


def get_published_service(
    session: ScopedSession,
    contents: Annotated[CourseContentsProvider, Depends(get_contents_provider)],
) -> PublishedMapService:
    return PublishedMapService(
        SqlAlchemyPublicationRepository(session),
        MapResolutionService(
            SqlAlchemyCourseMapRepository(session),
            SqlAlchemyBubbleRepository(session),
            contents,
        ),
    )


ServiceDep = Annotated[PublishedMapService, Depends(get_published_service)]


@router.get("", response_model=PublishedMapList)
async def list_published_maps(
    service: ServiceDep,
    moodle_course_id: Annotated[int | None, Query(alias="moodleCourseId")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublishedMapList:
    return await service.list_maps(
        moodle_course_id=moodle_course_id, limit=limit, offset=offset
    )


@router.get("/{course_map_id}", response_model=PublishedMapRead)
async def get_published_map(
    course_map_id: uuid.UUID, service: ServiceDep
) -> PublishedMapRead:
    return await service.get_map(course_map_id)


@router.get(
    "/{course_map_id}/resolved",
    response_model=ResolvedMap,
    dependencies=[Depends(moodle_source_route)],
)
async def resolve_published_map(
    course_map_id: uuid.UUID,
    service: ServiceDep,
    include_hidden: Annotated[bool, Query(alias="includeHidden")] = False,
) -> ResolvedMap:
    return await service.resolve(course_map_id, include_hidden=include_hidden)
