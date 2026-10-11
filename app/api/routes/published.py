"""The student-facing read API: published maps only.

SECURITY: there is no authentication, so this is not access control; it is the
consistent view students are given. `includeHidden` on `/resolved` has the same
caveat as on the editor's endpoint (see `course_map.py`).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
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

router = APIRouter(prefix="/published/course-maps", tags=["published"])


def get_published_service(
    session: Annotated[AsyncSession, Depends(get_session)],
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


@router.get("/{course_map_id}/resolved", response_model=ResolvedMap)
async def resolve_published_map(
    course_map_id: uuid.UUID,
    service: ServiceDep,
    # TODO(auth): like the editor's `/resolved`, anyone can read the names of
    # hidden activities with `includeHidden=true` until roles exist.
    include_hidden: Annotated[bool, Query(alias="includeHidden")] = False,
) -> ResolvedMap:
    return await service.resolve(course_map_id, include_hidden=include_hidden)
