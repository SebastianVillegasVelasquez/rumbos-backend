import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import CourseMapCreate, CourseMapDetail, CourseMapRead
from app.services.course_map_service import CourseMapService

router = APIRouter(prefix="/course-maps", tags=["course-maps"])


def get_course_map_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CourseMapService:
    return CourseMapService(
        SqlAlchemyCourseMapRepository(session), SqlAlchemyBubbleRepository(session)
    )


ServiceDep = Annotated[CourseMapService, Depends(get_course_map_service)]


@router.post("", response_model=CourseMapRead, status_code=status.HTTP_201_CREATED)
async def create_course_map(
    data: CourseMapCreate, service: ServiceDep
) -> CourseMapRead:
    return await service.create_course_map(data)


@router.get("/{course_map_id}", response_model=CourseMapDetail)
async def get_course_map(
    course_map_id: uuid.UUID, service: ServiceDep
) -> CourseMapDetail:
    return await service.get_course_map(course_map_id)


@router.post(
    "/{course_map_id}/bubbles",
    response_model=BubbleRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_bubble(
    course_map_id: uuid.UUID, data: BubbleCreate, service: ServiceDep
) -> BubbleRead:
    return await service.add_bubble(course_map_id, data)


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
