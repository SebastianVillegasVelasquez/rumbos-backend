"""In-memory test doubles for the repository and Moodle client Protocols.

No inheritance from the Protocols: they satisfy them structurally, which
mypy checks wherever a fake is passed to `CourseMapService`.
"""

import asyncio
import uuid
from collections.abc import Collection
from datetime import UTC, datetime

from uuid_utils.compat import uuid7

from app.exceptions import (
    ActivityAlreadyPlacedError,
    CourseMapNotFoundError,
    SectionAlreadyMappedError,
)
from app.moodle.exceptions import MoodleCourseNotFoundError
from app.moodle.schemas import MoodleSection, MoodleSiteInfo
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import (
    CourseMapBase,
    CourseMapCore,
    CourseMapCreate,
    CourseMapSummary,
    CourseMapUpdate,
)
from app.schemas.skin import ImageSkin, ProceduralSkin, SkinRead


class InMemoryCourseMapRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, CourseMapBase] = {}
        self.bubble_counts: dict[uuid.UUID, int] = {}
        self.complete_counts: dict[uuid.UUID, int] = {}

    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapBase | None:
        return self.items.get(course_map_id)

    def _level_order(self, moodle_course_id: int) -> list[CourseMapBase]:
        maps = [
            m for m in self.items.values() if m.moodle_course_id == moodle_course_id
        ]
        return sorted(maps, key=lambda m: (m.position, m.created_at, m.id))

    def _summary(self, m: CourseMapBase) -> CourseMapSummary:
        return CourseMapSummary(
            **CourseMapCore.model_validate(m).model_dump(),
            bubble_count=self.bubble_counts.get(m.id, 0),
            complete_count=self.complete_counts.get(m.id, 0),
        )

    async def list_for_course(self, moodle_course_id: int) -> list[CourseMapSummary]:
        return [self._summary(m) for m in self._level_order(moodle_course_id)]

    async def create(self, data: CourseMapCreate) -> CourseMapBase:
        siblings = self._level_order(data.moodle_course_id)
        if data.moodle_section_id is not None and any(
            m.moodle_section_id == data.moodle_section_id for m in siblings
        ):
            raise SectionAlreadyMappedError(data.moodle_section_id)
        now = datetime.now(UTC)
        item = CourseMapBase(
            id=uuid7(),
            created_at=now,
            updated_at=now,
            position=max((m.position for m in siblings), default=-1) + 1,
            **data.model_dump(),
        )
        self.items[item.id] = item
        return item

    async def set_order(
        self, moodle_course_id: int, ordered_ids: list[uuid.UUID]
    ) -> None:
        for position, map_id in enumerate(ordered_ids):
            current = self.items[map_id]
            if current.moodle_course_id == moodle_course_id:
                self.items[map_id] = current.model_copy(update={"position": position})

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
        if moodle_course_id is None:
            matches.sort(key=lambda m: (m.updated_at, m.id), reverse=True)
        else:
            matches.sort(key=lambda m: (m.position, m.created_at, m.id))
        page = [self._summary(m) for m in matches[offset : offset + limit]]
        return page, len(matches)


class InMemoryBubbleRepository:
    def __init__(self, course_maps: InMemoryCourseMapRepository) -> None:
        self.items: dict[uuid.UUID, BubbleRead] = {}
        self._course_maps = course_maps
        self.writes = 0  # counts write calls, to assert on what the service did

    async def get_by_id(self, bubble_id: uuid.UUID) -> BubbleRead | None:
        return self.items.get(bubble_id)

    async def list_by_course_map(self, course_map_id: uuid.UUID) -> list[BubbleRead]:
        own = [b for b in self.items.values() if b.course_map_id == course_map_id]
        return sorted(own, key=lambda b: (b.sequence, b.created_at, b.id))

    async def list_by_course(self, moodle_course_id: int) -> list[BubbleRead]:
        return [
            b
            for b in self.items.values()
            if self._course_maps.items[b.course_map_id].moodle_course_id
            == moodle_course_id
        ]

    async def create(
        self, course_map_id: uuid.UUID, moodle_course_id: int, data: BubbleCreate
    ) -> BubbleRead:
        if course_map_id not in self._course_maps.items:
            raise CourseMapNotFoundError(course_map_id)
        for other in await self.list_by_course(moodle_course_id):
            if other.activity_id == data.activity_id:
                owner = self._course_maps.items[other.course_map_id]
                raise ActivityAlreadyPlacedError(
                    data.activity_id, owner.id, owner.title
                )
        self.writes += 1
        counts = self._course_maps.bubble_counts
        counts[course_map_id] = counts.get(course_map_id, 0) + 1
        now = datetime.now(UTC)
        siblings = await self.list_by_course_map(course_map_id)
        item = BubbleRead(
            id=uuid7(),
            course_map_id=course_map_id,
            skin_id=None,
            sequence=max((b.sequence for b in siblings), default=-1) + 1,
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


class InMemorySkinRepository:
    """Knows which skin ids exist; `service` tests only need that."""

    def __init__(self) -> None:
        self.ids: set[uuid.UUID] = set()

    def add(self) -> uuid.UUID:
        skin_id: uuid.UUID = uuid7()
        self.ids.add(skin_id)
        return skin_id

    async def get_by_id(self, skin_id: uuid.UUID) -> SkinRead | None:
        raise NotImplementedError

    async def existing_ids(self, skin_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
        return {i for i in skin_ids if i in self.ids}

    async def create(self, name: str, config: ProceduralSkin | ImageSkin) -> SkinRead:
        raise NotImplementedError

    async def update(
        self,
        skin_id: uuid.UUID,
        name: str | None,
        config: ProceduralSkin | ImageSkin | None,
    ) -> SkinRead | None:
        raise NotImplementedError

    async def delete(self, skin_id: uuid.UUID) -> bool:
        raise NotImplementedError

    async def list(self, limit: int) -> list[SkinRead]:
        raise NotImplementedError


class InMemoryMoodleClient:
    """Serves canned course contents; no HTTP.

    Set `error` to make every call raise it, to simulate Moodle failures.
    """

    def __init__(self) -> None:
        self.courses: dict[int, list[MoodleSection]] = {}
        self.error: Exception | None = None
        self.calls = 0  # get_course_contents calls that reached this fake
        # When set, get_course_contents waits for it: lets a test hold a call
        # open while other callers pile up.
        self.gate: asyncio.Event | None = None

    async def get_site_info(self) -> MoodleSiteInfo:
        if self.error:
            raise self.error
        return MoodleSiteInfo(sitename="Fake Moodle")

    async def get_course_contents(self, course_id: int) -> list[MoodleSection]:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        if course_id not in self.courses:
            raise MoodleCourseNotFoundError("Moodle course not found")
        return self.courses[course_id]


class FakeClock:
    """A manually advanced monotonic clock, for cache tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
