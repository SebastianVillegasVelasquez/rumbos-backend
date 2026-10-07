import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CourseMapAlreadyExistsError
from app.models import Bubble, CourseMap
from app.schemas.course_map import CourseMapBase, CourseMapCreate, CourseMapSummary


class SqlAlchemyCourseMapRepository:
    """Implements `CourseMapRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapBase | None:
        course_map = await self._session.get(CourseMap, course_map_id)
        return CourseMapBase.model_validate(course_map) if course_map else None

    async def get_by_moodle_course_id(
        self, moodle_course_id: int
    ) -> CourseMapBase | None:
        course_map = await self._session.scalar(
            select(CourseMap).where(CourseMap.moodle_course_id == moodle_course_id)
        )
        return CourseMapBase.model_validate(course_map) if course_map else None

    async def list(
        self,
        moodle_course_id: int | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[CourseMapSummary], int]:
        filters = []
        if moodle_course_id is not None:
            filters.append(CourseMap.moodle_course_id == moodle_course_id)
        if q:
            # `autoescape` makes "%" and "_" in the search text literal.
            filters.append(CourseMap.title.icontains(q, autoescape=True))

        total = await self._session.scalar(
            select(func.count()).select_from(CourseMap).where(*filters)
        )
        # One aggregate joined in, so counting bubbles never costs a query per
        # map (and never touches the `lazy="raise"` relationship).
        counts = (
            select(Bubble.course_map_id, func.count().label("n"))
            .group_by(Bubble.course_map_id)
            .subquery()
        )
        rows = await self._session.execute(
            select(CourseMap, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.course_map_id == CourseMap.id)
            .where(*filters)
            # `id` (UUIDv7) makes the order total when timestamps tie.
            .order_by(CourseMap.updated_at.desc(), CourseMap.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items = [
            CourseMapSummary(
                **CourseMapBase.model_validate(course_map).model_dump(),
                bubble_count=bubble_count,
            )
            for course_map, bubble_count in rows
        ]
        return items, total or 0

    async def create(self, data: CourseMapCreate) -> CourseMapBase:
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
        return CourseMapBase.model_validate(course_map)

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
