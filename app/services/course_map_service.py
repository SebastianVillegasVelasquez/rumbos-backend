import uuid

from app.exceptions import (
    BubbleNotFoundError,
    CourseMapAlreadyExistsError,
    CourseMapNotFoundError,
)
from app.repositories.protocols import BubbleRepository, CourseMapRepository
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import CourseMapCreate, CourseMapDetail, CourseMapRead


class CourseMapService:
    """Orchestrates course maps and bubbles.

    Depends only on the repository Protocols, never on SQLAlchemy, so it can
    be tested against in-memory fakes. Transaction boundaries live in the
    repositories.
    """

    def __init__(
        self, course_maps: CourseMapRepository, bubbles: BubbleRepository
    ) -> None:
        self._course_maps = course_maps
        self._bubbles = bubbles

    async def create_course_map(self, data: CourseMapCreate) -> CourseMapRead:
        if await self._course_maps.get_by_moodle_course_id(data.moodle_course_id):
            raise CourseMapAlreadyExistsError(data.moodle_course_id)
        # The repository still enforces uniqueness, covering the race between
        # this check and the insert.
        return await self._course_maps.create(data)

    async def get_course_map(self, course_map_id: uuid.UUID) -> CourseMapDetail:
        course_map = await self._require_course_map(course_map_id)
        bubbles = await self._bubbles.list_by_course_map(course_map_id)
        return CourseMapDetail(**course_map.model_dump(), bubbles=bubbles)

    async def add_bubble(
        self, course_map_id: uuid.UUID, data: BubbleCreate
    ) -> BubbleRead:
        await self._require_course_map(course_map_id)
        return await self._bubbles.create(course_map_id, data)

    async def update_bubble(
        self, course_map_id: uuid.UUID, bubble_id: uuid.UUID, data: BubbleUpdate
    ) -> BubbleRead:
        """Applies only the fields present in `data` (partial update)."""
        await self._require_bubble(course_map_id, bubble_id)
        updated = await self._bubbles.update(bubble_id, data)
        if updated is None:  # deleted between the lookup and the write
            raise BubbleNotFoundError(bubble_id)
        return updated

    async def remove_bubble(
        self, course_map_id: uuid.UUID, bubble_id: uuid.UUID
    ) -> None:
        await self._require_bubble(course_map_id, bubble_id)
        if not await self._bubbles.delete(bubble_id):
            raise BubbleNotFoundError(bubble_id)

    async def _require_course_map(self, course_map_id: uuid.UUID) -> CourseMapRead:
        course_map = await self._course_maps.get_by_id(course_map_id)
        if course_map is None:
            raise CourseMapNotFoundError(course_map_id)
        return course_map

    async def _require_bubble(
        self, course_map_id: uuid.UUID, bubble_id: uuid.UUID
    ) -> BubbleRead:
        """A bubble addressed through the wrong map counts as not found."""
        bubble = await self._bubbles.get_by_id(bubble_id)
        if bubble is None or bubble.course_map_id != course_map_id:
            raise BubbleNotFoundError(bubble_id)
        return bubble
