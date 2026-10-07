"""In-memory test doubles for the repository and Moodle client Protocols.

No inheritance from the Protocols: they satisfy them structurally, which
mypy checks wherever a fake is passed to `CourseMapService`.
"""

import uuid
from datetime import UTC, datetime

from uuid_utils.compat import uuid7

from app.exceptions import (
    ActivityAlreadyPlacedError,
    CourseMapAlreadyExistsError,
    CourseMapNotFoundError,
)
from app.moodle.exceptions import MoodleCourseNotFoundError
from app.moodle.schemas import MoodleSection, MoodleSiteInfo
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import (
    CourseMapBase,
    CourseMapCreate,
    CourseMapSummary,
    CourseMapUpdate,
)


class InMemoryCourseMapRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, CourseMapBase] = {}
        self.bubble_counts: dict[uuid.UUID, int] = {}

    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapBase | None:
        return self.items.get(course_map_id)

    async def get_by_moodle_course_id(
        self, moodle_course_id: int
    ) -> CourseMapBase | None:
        return next(
            (m for m in self.items.values() if m.moodle_course_id == moodle_course_id),
            None,
        )

    async def list(
        self,
        moodle_course_id: int | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[CourseMapSummary], int]:
        matches = [
            m
            for m in self.items.values()
            if (moodle_course_id is None or m.moodle_course_id == moodle_course_id)
            and (not q or q.lower() in m.title.lower())
        ]
        matches.sort(key=lambda m: (m.updated_at, m.id), reverse=True)
        page = [
            CourseMapSummary(
                **m.model_dump(), bubble_count=self.bubble_counts.get(m.id, 0)
            )
            for m in matches[offset : offset + limit]
        ]
        return page, len(matches)

    async def create(self, data: CourseMapCreate) -> CourseMapBase:
        if await self.get_by_moodle_course_id(data.moodle_course_id):
            raise CourseMapAlreadyExistsError(data.moodle_course_id)
        now = datetime.now(UTC)
        item = CourseMapBase(
            id=uuid7(), created_at=now, updated_at=now, **data.model_dump()
        )
        self.items[item.id] = item
        return item

    async def update(
        self, course_map_id: uuid.UUID, data: CourseMapUpdate
    ) -> CourseMapBase | None:
        current = self.items.get(course_map_id)
        if current is None:
            return None
        changes = data.model_dump(exclude_unset=True)
        updated = current.model_copy(
            update={**changes, "updated_at": datetime.now(UTC)}
        )
        self.items[course_map_id] = updated
        return updated

    async def delete(self, course_map_id: uuid.UUID) -> bool:
        return self.items.pop(course_map_id, None) is not None


class InMemoryBubbleRepository:
    def __init__(self, course_maps: InMemoryCourseMapRepository) -> None:
        self.items: dict[uuid.UUID, BubbleRead] = {}
        self._course_maps = course_maps
        self.writes = 0  # counts write calls, to assert on what the service did

    async def get_by_id(self, bubble_id: uuid.UUID) -> BubbleRead | None:
        return self.items.get(bubble_id)

    async def list_by_course_map(self, course_map_id: uuid.UUID) -> list[BubbleRead]:
        return [b for b in self.items.values() if b.course_map_id == course_map_id]

    async def create(self, course_map_id: uuid.UUID, data: BubbleCreate) -> BubbleRead:
        if course_map_id not in self._course_maps.items:
            raise CourseMapNotFoundError(course_map_id)
        if any(
            b.course_map_id == course_map_id and b.activity_id == data.activity_id
            for b in self.items.values()
        ):
            raise ActivityAlreadyPlacedError(data.activity_id)
        self.writes += 1
        counts = self._course_maps.bubble_counts
        counts[course_map_id] = counts.get(course_map_id, 0) + 1
        now = datetime.now(UTC)
        item = BubbleRead(
            id=uuid7(),
            course_map_id=course_map_id,
            created_at=now,
            updated_at=now,
            **data.model_dump(),
        )
        self.items[item.id] = item
        return item

    async def update(
        self, bubble_id: uuid.UUID, data: BubbleUpdate
    ) -> BubbleRead | None:
        current = self.items.get(bubble_id)
        if current is None:
            return None
        self.writes += 1
        changes = data.model_dump(exclude_unset=True)
        updated = current.model_copy(
            update={**changes, "updated_at": datetime.now(UTC)}
        )
        self.items[bubble_id] = updated
        return updated

    async def delete(self, bubble_id: uuid.UUID) -> bool:
        self.writes += 1
        bubble = self.items.pop(bubble_id, None)
        if bubble is not None:
            self._course_maps.bubble_counts[bubble.course_map_id] -= 1
        return bubble is not None


class InMemoryMoodleClient:
    """Serves canned course contents; no HTTP.

    Set `error` to make every call raise it, to simulate Moodle failures.
    """

    def __init__(self) -> None:
        self.courses: dict[int, list[MoodleSection]] = {}
        self.error: Exception | None = None

    async def get_site_info(self) -> MoodleSiteInfo:
        if self.error:
            raise self.error
        return MoodleSiteInfo(sitename="Fake Moodle")

    async def get_course_contents(self, course_id: int) -> list[MoodleSection]:
        if self.error:
            raise self.error
        if course_id not in self.courses:
            raise MoodleCourseNotFoundError("Moodle course not found")
        return self.courses[course_id]
