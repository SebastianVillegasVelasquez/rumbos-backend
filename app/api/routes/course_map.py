import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.moodle.dependencies import get_contents_provider, get_moodle_client
from app.moodle.protocols import CourseContentsProvider, MoodleClient
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.repositories.sqlalchemy.skin_repository import SqlAlchemySkinRepository
from app.schemas.activity import ActivityRead
from app.schemas.bubble import (
    BubbleCreate,
    BubbleOrdered,
    BubbleOrderUpdate,
    BubbleRead,
    BubbleUpdate,
)
from app.schemas.course_map import (
    AppearanceUpdate,
    CourseMapCreate,
    CourseMapList,
    CourseMapOrdered,
    CourseMapOrderUpdate,
    CourseMapRead,
    CourseMapUpdate,
)
from app.schemas.resolved import ResolvedMap
from app.services.course_map_service import CourseMapService
from app.services.resolution_service import MapResolutionService

router = APIRouter(prefix="/course-maps", tags=["course-maps"])


def get_course_map_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    moodle: Annotated[MoodleClient, Depends(get_moodle_client)],
) -> CourseMapService:
    return CourseMapService(
        SqlAlchemyCourseMapRepository(session),
        SqlAlchemyBubbleRepository(session),
        moodle,
        SqlAlchemySkinRepository(session),
    )


def get_resolution_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    contents: Annotated[CourseContentsProvider, Depends(get_contents_provider)],
) -> MapResolutionService:
    return MapResolutionService(
        SqlAlchemyCourseMapRepository(session),
        SqlAlchemyBubbleRepository(session),
        contents,
    )


ServiceDep = Annotated[CourseMapService, Depends(get_course_map_service)]
ResolutionServiceDep = Annotated[MapResolutionService, Depends(get_resolution_service)]


@router.get("", response_model=CourseMapList)
async def list_course_maps(
    service: ServiceDep,
    moodle_course_id: Annotated[int | None, Query(alias="moodleCourseId")] = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CourseMapList:
    return await service.list_course_maps(
        moodle_course_id=moodle_course_id, q=q, limit=limit, offset=offset
    )


@router.post("", response_model=CourseMapRead, status_code=status.HTTP_201_CREATED)
async def create_course_map(
    data: CourseMapCreate, service: ServiceDep
) -> CourseMapRead:
    return await service.create_course_map(data)


@router.put("/order", response_model=CourseMapOrdered)
async def reorder_course_maps(
    data: CourseMapOrderUpdate, service: ServiceDep
) -> CourseMapOrdered:
    return await service.reorder_course_maps(data)


@router.get("/{course_map_id}", response_model=CourseMapRead)
async def get_course_map(
    course_map_id: uuid.UUID, service: ServiceDep
) -> CourseMapRead:
    return await service.get_course_map(course_map_id)


@router.patch("/{course_map_id}", response_model=CourseMapRead)
async def update_course_map(
    course_map_id: uuid.UUID, data: CourseMapUpdate, service: ServiceDep
) -> CourseMapRead:
    return await service.update_course_map(course_map_id, data)


@router.put("/{course_map_id}/appearance", response_model=CourseMapRead)
async def update_appearance(
    course_map_id: uuid.UUID, data: AppearanceUpdate, service: ServiceDep
) -> CourseMapRead:
    return await service.update_appearance(course_map_id, data)


@router.delete("/{course_map_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_course_map(course_map_id: uuid.UUID, service: ServiceDep) -> Response:
    await service.delete_course_map(course_map_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{course_map_id}/activities", response_model=list[ActivityRead])
async def list_activities(
    course_map_id: uuid.UUID,
    service: ServiceDep,
    # TODO(auth): there is no authentication yet, so anyone can list the names
    # of hidden activities with `includeHidden=true` (here and on `/resolved`).
    # When auth arrives, this parameter must require a teacher/editor role.
    include_hidden: Annotated[bool, Query(alias="includeHidden")] = False,
    only_section: Annotated[bool, Query(alias="onlySection")] = False,
) -> list[ActivityRead]:
    return await service.list_activities(
        course_map_id, include_hidden=include_hidden, only_section=only_section
    )


@router.get("/{course_map_id}/resolved", response_model=ResolvedMap)
async def resolve_course_map(
    course_map_id: uuid.UUID,
    service: ResolutionServiceDep,
    # TODO(auth): same as `/activities`: until there is authentication anyone
    # can read the names of hidden activities with `includeHidden=true`. It
    # must require a teacher/editor role once roles exist.
    include_hidden: Annotated[bool, Query(alias="includeHidden")] = False,
) -> ResolvedMap:
    return await service.resolve(course_map_id, include_hidden=include_hidden)


@router.post(
    "/{course_map_id}/bubbles",
    response_model=BubbleRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_bubble(
    course_map_id: uuid.UUID, data: BubbleCreate, service: ServiceDep
) -> BubbleRead:
    return await service.add_bubble(course_map_id, data)


@router.put("/{course_map_id}/bubbles/order", response_model=BubbleOrdered)
async def reorder_bubbles(
    course_map_id: uuid.UUID, data: BubbleOrderUpdate, service: ServiceDep
) -> BubbleOrdered:
    return await service.reorder_bubbles(course_map_id, data)


@router.patch("/{course_map_id}/bubbles/{bubble_id}", response_model=BubbleRead)
async def update_bubble(
    course_map_id: uuid.UUID,
    bubble_id: uuid.UUID,
    data: BubbleUpdate,
    service: ServiceDep,
) -> BubbleRead:
    return await service.update_bubble(course_map_id, bubble_id, data)


@router.delete(
    "/{course_map_id}/bubbles/{bubble_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_bubble(
    course_map_id: uuid.UUID, bubble_id: uuid.UUID, service: ServiceDep
) -> Response:
    await service.remove_bubble(course_map_id, bubble_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
