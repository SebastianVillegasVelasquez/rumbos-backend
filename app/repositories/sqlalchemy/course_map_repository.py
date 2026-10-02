import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CourseMapAlreadyExistsError
from app.models import CourseMap
from app.schemas.course_map import CourseMapCreate, CourseMapRead


class SqlAlchemyCourseMapRepository:
    """Implements `CourseMapRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapRead | None:
        course_map = await self._session.get(CourseMap, course_map_id)
        return CourseMapRead.model_validate(course_map) if course_map else None

    async def get_by_moodle_course_id(
        self, moodle_course_id: int
    ) -> CourseMapRead | None:
        course_map = await self._session.scalar(
            select(CourseMap).where(CourseMap.moodle_course_id == moodle_course_id)
        )
        return CourseMapRead.model_validate(course_map) if course_map else None

    async def create(self, data: CourseMapCreate) -> CourseMapRead:
        course_map = CourseMap(**data.model_dump())
        self._session.add(course_map)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise CourseMapAlreadyExistsError(data.moodle_course_id) from exc
        except BaseException:
            await self._session.rollback()
            raise
        return CourseMapRead.model_validate(course_map)

    async def delete(self, course_map_id: uuid.UUID) -> bool:
        course_map = await self._session.get(CourseMap, course_map_id)
        if course_map is None:
            return False
        try:
            await self._session.delete(course_map)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return True
