import uuid

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    ActivityAlreadyPlacedError,
    CourseMapNotFoundError,
    SkinReferenceNotFoundError,
    VersionConflictError,
)
from app.models import Bubble, CourseMap
from app.models.bubble import UQ_BUBBLE_ACTIVITY
from app.repositories.sqlalchemy._errors import (
    FOREIGN_KEY_VIOLATION,
    constraint_name,
    sqlstate,
)
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate

# The guided path order. `created_at, id` break ties (duplicate sequences are
# tolerated; the order endpoint normalizes them).
_PATH_ORDER = (Bubble.sequence, Bubble.created_at, Bubble.id)


class SqlAlchemyBubbleRepository:
    """Implements `BubbleRepository`. Writes commit; failures roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, bubble_id: uuid.UUID) -> BubbleRead | None:
        bubble = await self._session.get(Bubble, bubble_id)
        return BubbleRead.model_validate(bubble) if bubble else None

    async def list_by_course_map(self, course_map_id: uuid.UUID) -> list[BubbleRead]:
        result = await self._session.scalars(
            select(Bubble)
            .where(Bubble.course_map_id == course_map_id)
            .order_by(*_PATH_ORDER)
            .execution_options(populate_existing=True)
        )
        return [BubbleRead.model_validate(b) for b in result]

    async def list_by_course(self, moodle_course_id: int) -> list[BubbleRead]:
        result = await self._session.scalars(
            select(Bubble)
            .where(Bubble.moodle_course_id == moodle_course_id)
            .order_by(*_PATH_ORDER)
        )
        return [BubbleRead.model_validate(b) for b in result]

    async def create(
        self, course_map_id: uuid.UUID, moodle_course_id: int, data: BubbleCreate
    ) -> BubbleRead:
        try:
            # New bubbles go last: max + 1 in this transaction (a simultaneous
            # create can tie; that is tolerated, see `_PATH_ORDER`).
            last = await self._session.scalar(
                select(func.max(Bubble.sequence)).where(
                    Bubble.course_map_id == course_map_id
                )
            )
            bubble = Bubble(
                course_map_id=course_map_id,
                moodle_course_id=moodle_course_id,
                sequence=0 if last is None else last + 1,
                **data.model_dump(),
            )
            self._session.add(bubble)
            await self._session.commit()
        except IntegrityError as exc:
            # Inputs are validated upstream, so what is left is a foreign key
            # to a map that doesn't exist, or the one-bubble-per-activity-per-
            # course constraint (which is what stops concurrent duplicates).
            await self._session.rollback()
            if constraint_name(exc) == UQ_BUBBLE_ACTIVITY and data.activity_id:
                raise await self._already_placed(
                    moodle_course_id, data.activity_id
                ) from exc
            if sqlstate(exc) == FOREIGN_KEY_VIOLATION:
                raise CourseMapNotFoundError(course_map_id) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return BubbleRead.model_validate(bubble)

    async def _already_placed(
        self, moodle_course_id: int, activity_id: int
    ) -> ActivityAlreadyPlacedError:
        """The error for a duplicate, naming the map that already has it."""
        # This read starts a new transaction (the failed one was rolled back);
        # close it so the repository leaves the session clean.
        row = (
            await self._session.execute(
                select(CourseMap.id, CourseMap.title)
                .join(Bubble, Bubble.course_map_id == CourseMap.id)
                .where(
                    Bubble.moodle_course_id == moodle_course_id,
                    Bubble.activity_id == activity_id,
                )
            )
        ).first()
        await self._session.rollback()
        if row is None:  # the other bubble was deleted since the violation
            return ActivityAlreadyPlacedError(activity_id)
        return ActivityAlreadyPlacedError(activity_id, row.id, row.title)

    async def _stale_or_missing(self, bubble_id: uuid.UUID) -> None:
        """After a guarded write matched no row: raise `VersionConflictError` if
        the bubble exists (stale version); return if it doesn't. Ends the
        transaction."""
        current = await self._session.scalar(
            select(Bubble.version).where(Bubble.id == bubble_id)
        )
        await self._session.rollback()
        if current is not None:
            raise VersionConflictError(current_version=current)

    async def update(
        self, bubble_id: uuid.UUID, data: BubbleUpdate, expected_version: int | None
    ) -> BubbleRead | None:
        # One UPDATE touching only the fields the caller sent (an explicit
        # `icon: None` is sent, so it clears the column). When guarded, the same
        # statement checks and bumps the version; unguarded (status only) it
        # touches nothing but the status, so it cannot conflict with an edit.
        values = data.model_dump(exclude_unset=True)
        stmt = update(Bubble).where(Bubble.id == bubble_id)
        if expected_version is None:
            stmt = stmt.values(**values)
        else:
            stmt = stmt.where(Bubble.version == expected_version).values(
                **values, version=Bubble.version + 1
            )
        stmt = stmt.returning(Bubble).execution_options(populate_existing=True)
        try:
            bubble = (await self._session.scalars(stmt)).one_or_none()
            if bubble is None and expected_version is not None:
                await self._stale_or_missing(bubble_id)
                return None
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            # The skin was deleted after the service checked it.
            if sqlstate(exc) == FOREIGN_KEY_VIOLATION and values.get("skin_id"):
                raise SkinReferenceNotFoundError([values["skin_id"]]) from exc
            raise
        except BaseException:
            await self._session.rollback()
            raise
        return BubbleRead.model_validate(bubble) if bubble else None

    async def set_order(
        self, course_map_id: uuid.UUID, ordered_ids: list[uuid.UUID]
    ) -> None:
        if not ordered_ids:
            return  # nothing to order (and a CASE needs at least one branch)
        # One UPDATE with a CASE: the whole reorder is a single statement.
        sequences = case(
            {bubble_id: index for index, bubble_id in enumerate(ordered_ids)},
            value=Bubble.id,
        )
        stmt = (
            update(Bubble)
            .where(Bubble.course_map_id == course_map_id, Bubble.id.in_(ordered_ids))
            .values(sequence=sequences)
            .execution_options(synchronize_session="fetch")
        )
        try:
            await self._session.execute(stmt)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise

    async def delete(self, bubble_id: uuid.UUID, expected_version: int) -> bool:
        stmt = (
            delete(Bubble)
            .where(Bubble.id == bubble_id, Bubble.version == expected_version)
            .returning(Bubble.id)
            .execution_options(synchronize_session=False)
        )
        try:
            deleted = (await self._session.execute(stmt)).scalar_one_or_none()
            if deleted is None:
                await self._stale_or_missing(bubble_id)
                return False
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return True
