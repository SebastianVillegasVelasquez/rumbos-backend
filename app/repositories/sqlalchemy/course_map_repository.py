import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import BubbleStatus
from app.exceptions import SectionAlreadyMappedError, SkinReferenceNotFoundError
from app.models import Bubble, CourseMap, CourseMapSkinRule
from app.models.course_map import UQ_COURSE_MAP_SECTION
from app.repositories.sqlalchemy._errors import (
    FOREIGN_KEY_VIOLATION,
    constraint_name,
    sqlstate,
)
from app.schemas.course_map import (
    CourseMapBase,
    CourseMapCore,
    CourseMapCreate,
    CourseMapSummary,
    CourseMapUpdate,
    SkinRule,
)
from app.schemas.map_settings import MapSettings


class SqlAlchemyCourseMapRepository:
    """Implements `CourseMapRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapBase | None:
        course_map = await self._session.get(CourseMap, course_map_id)
        return CourseMapBase.model_validate(course_map) if course_map else None

    async def list_for_course(self, moodle_course_id: int) -> list[CourseMapSummary]:
        return await self._summaries(
            [CourseMap.moodle_course_id == moodle_course_id],
            (CourseMap.position, CourseMap.created_at, CourseMap.id),
        )

    async def _summaries(
        self,
        filters: Sequence[ColumnElement[bool]],
        order_by: Sequence[Any],
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CourseMapSummary]:
        # One aggregate joined in, so counting bubbles never costs a query per
        # map (and never touches the `lazy="raise"` relationship).
        counts = (
            select(
                Bubble.course_map_id,
                func.count().label("n"),
                func.count()
                .filter(Bubble.status == BubbleStatus.COMPLETE)
                .label("done"),
            )
            .group_by(Bubble.course_map_id)
            .subquery()
        )
        stmt = (
            select(
                CourseMap,
                func.coalesce(counts.c.n, 0),
                func.coalesce(counts.c.done, 0),
            )
            .outerjoin(counts, counts.c.course_map_id == CourseMap.id)
            .where(*filters)
            .order_by(*order_by)
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = await self._session.execute(stmt)
        return [
            CourseMapSummary(
                **CourseMapCore.model_validate(course_map).model_dump(),
                bubble_count=bubble_count,
                complete_count=complete_count,
            )
            for course_map, bubble_count, complete_count in rows
        ]

    async def create(self, data: CourseMapCreate) -> CourseMapBase:
        try:
            # Last + 1 within this transaction. Two simultaneous creates can
            # pick the same position; that is tolerated (see `CourseMap`).
            last = await self._session.scalar(
                select(func.max(CourseMap.position)).where(
                    CourseMap.moodle_course_id == data.moodle_course_id
                )
            )
            course_map = CourseMap(
                **data.model_dump(),
                position=0 if last is None else last + 1,
                settings=MapSettings().to_stored(),
            )
            self._session.add(course_map)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if constraint_name(exc) == UQ_COURSE_MAP_SECTION:
                raise SectionAlreadyMappedError(data.moodle_section_id) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return CourseMapBase.model_validate(course_map)

    async def update(
        self, course_map_id: uuid.UUID, data: CourseMapUpdate
    ) -> CourseMapBase | None:
        # One UPDATE touching only the fields the caller sent; `updated_at`
        # is refreshed by the column's `onupdate`.
        values = data.model_dump(exclude_unset=True)
        stmt = (
            update(CourseMap)
            .where(CourseMap.id == course_map_id)
            .values(**values)
            .returning(CourseMap)
            .execution_options(populate_existing=True)
        )
        try:
            course_map = (await self._session.scalars(stmt)).one_or_none()
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return CourseMapBase.model_validate(course_map) if course_map else None

    async def set_order(
        self, moodle_course_id: int, ordered_ids: list[uuid.UUID]
    ) -> None:
        if not ordered_ids:
            return  # nothing to order (and a CASE needs at least one branch)
        # One UPDATE with a CASE, so the whole reorder is a single statement.
        positions = case(
            {map_id: index for index, map_id in enumerate(ordered_ids)},
            value=CourseMap.id,
        )
        stmt = (
            update(CourseMap)
            .where(
                CourseMap.moodle_course_id == moodle_course_id,
                CourseMap.id.in_(ordered_ids),
            )
            .values(position=positions)
            .execution_options(synchronize_session="fetch")
        )
        try:
            await self._session.execute(stmt)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise

    async def replace_appearance(
        self,
        course_map_id: uuid.UUID,
        settings: MapSettings,
        default_skin_id: uuid.UUID | None,
        rules: list[SkinRule],
    ) -> bool:
        # Every statement below runs in the session's single transaction and
        # the one commit at the end publishes all of it; any failure rolls
        # everything back (a bad rule never leaves new settings behind).
        try:
            updated = await self._session.execute(
                update(CourseMap)
                .where(CourseMap.id == course_map_id)
                .values(settings=settings.to_stored(), default_skin_id=default_skin_id)
                .returning(CourseMap.id)
                .execution_options(synchronize_session="fetch")
            )
            if updated.scalar_one_or_none() is None:
                await self._session.rollback()
                return False
            await self._session.execute(
                delete(CourseMapSkinRule).where(
                    CourseMapSkinRule.course_map_id == course_map_id
                )
            )
            self._session.add_all(
                CourseMapSkinRule(
                    course_map_id=course_map_id,
                    modname=rule.modname,
                    skin_id=rule.skin_id,
                )
                for rule in rules
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if sqlstate(exc) == FOREIGN_KEY_VIOLATION:
                # A skin was deleted after the service checked it.
                wanted = [r.skin_id for r in rules]
                raise SkinReferenceNotFoundError(wanted) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return True

    async def list_skin_rules(self, course_map_id: uuid.UUID) -> list[SkinRule]:
        result = await self._session.scalars(
            select(CourseMapSkinRule)
            .where(CourseMapSkinRule.course_map_id == course_map_id)
            .order_by(CourseMapSkinRule.modname)
            .execution_options(populate_existing=True)
        )
        return [SkinRule.model_validate(rule) for rule in result]

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

    async def list(
        self,
        moodle_course_id: int | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[CourseMapSummary], int]:
        filters: list[ColumnElement[bool]] = []
        if moodle_course_id is not None:
            filters.append(CourseMap.moodle_course_id == moodle_course_id)
        if q:
            # `autoescape` makes "%" and "_" in the search text literal.
            filters.append(CourseMap.title.icontains(q, autoescape=True))

        total = await self._session.scalar(
            select(func.count()).select_from(CourseMap).where(*filters)
        )
        # `id` (UUIDv7) makes the order total when other keys tie.
        order_by: Sequence[Any] = (
            (CourseMap.position, CourseMap.created_at, CourseMap.id)
            if moodle_course_id is not None
            else (CourseMap.updated_at.desc(), CourseMap.id.desc())
        )
        items = await self._summaries(filters, order_by, limit=limit, offset=offset)
        return items, total or 0
