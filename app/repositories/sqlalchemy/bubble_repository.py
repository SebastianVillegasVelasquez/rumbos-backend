import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import CourseMapNotFoundError
from app.models import Bubble
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate


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
            .order_by(Bubble.id)  # UUIDv7 ids sort by creation time
        )
        return [BubbleRead.model_validate(b) for b in result]

    async def create(self, course_map_id: uuid.UUID, data: BubbleCreate) -> BubbleRead:
        bubble = Bubble(course_map_id=course_map_id, **data.model_dump())
        self._session.add(bubble)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            # Inputs are validated upstream, so the only way to violate a
            # constraint here is a foreign key to a map that doesn't exist.
            await self._session.rollback()
            raise CourseMapNotFoundError(course_map_id) from exc
        except BaseException:
            await self._session.rollback()
            raise
        return BubbleRead.model_validate(bubble)

    async def update(
        self, bubble_id: uuid.UUID, data: BubbleUpdate
    ) -> BubbleRead | None:
        # One UPDATE touching only the fields the caller sent (an explicit
        # `icon: None` is sent, so it clears the column).
        values = data.model_dump(exclude_unset=True)
        stmt = (
            update(Bubble)
            .where(Bubble.id == bubble_id)
            .values(**values)
            .returning(Bubble)
            .execution_options(populate_existing=True)
        )
        try:
            bubble = (await self._session.scalars(stmt)).one_or_none()
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return BubbleRead.model_validate(bubble) if bubble else None

    async def delete(self, bubble_id: uuid.UUID) -> bool:
        bubble = await self._session.get(Bubble, bubble_id)
        if bubble is None:
            return False
        try:
            await self._session.delete(bubble)
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        return True
