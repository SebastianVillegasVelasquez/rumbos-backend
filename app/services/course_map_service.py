import uuid

from app.exceptions import (
    BubbleNotFoundError,
    CourseMapAlreadyExistsError,
    CourseMapNotFoundError,
)
from app.moodle.protocols import MoodleClient
from app.moodle.schemas import MoodleModule, MoodleSection
from app.repositories.protocols import BubbleRepository, CourseMapRepository
from app.schemas.activity import ActivityRead
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import CourseMapCreate, CourseMapDetail, CourseMapRead


def is_bubble_candidate(module: MoodleModule) -> bool:
    """Whether a Moodle module is meaningful as a bubble on the map.

    A candidate is a module that is not a `label` (just text on the course
    page), has a `url`, and does not have `noviewlink` set (nothing to open).

    Deliberately NOT used: `visibleoncoursepage == 0` ("stealth" activities,
    reachable by link but not listed) stays a candidate, and `candisplay` is
    not consulted. Hiding is a separate concern, see `is_hidden`.

    This is the single place to change the candidate rule.
    """
    return module.modname != "label" and bool(module.url) and not module.noviewlink


def is_hidden(section: MoodleSection, module: MoodleModule) -> bool:
    """Whether learners would not see this module.

    True if the section is not visible, the module is not visible, or
    `uservisible` is false. `uservisible` alone is not trusted: the service
    token belongs to a privileged user who can see hidden content, so Moodle
    reports `uservisible: true` for things learners cannot see. A hidden
    section hides all its modules even when each module says `visible: 1`.
    """
    return not (section.visible and module.visible and module.uservisible)


class CourseMapService:
    """Orchestrates course maps and bubbles.

    Depends only on the repository and Moodle client Protocols, never on
    SQLAlchemy or httpx, so it can be tested against in-memory fakes.
    Transaction boundaries live in the repositories.
    """

    def __init__(
        self,
        course_maps: CourseMapRepository,
        bubbles: BubbleRepository,
        moodle: MoodleClient,
    ) -> None:
        self._course_maps = course_maps
        self._bubbles = bubbles
        self._moodle = moodle

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

    async def list_activities(
        self, course_map_id: uuid.UUID, *, include_hidden: bool = False
    ) -> list[ActivityRead]:
        """The map's Moodle course activities, flattened in Moodle's order.

        Order is section number, then position in the section's `modules`
        list. Each activity says whether a bubble on this map already
        references it (by module id) and whether learners would not see it.
        Hidden activities are left out unless `include_hidden`.
        Raises the `Moodle*Error`s from the client unchanged.
        """
        course_map = await self._require_course_map(course_map_id)
        bubbles = await self._bubbles.list_by_course_map(course_map_id)
        sections = await self._moodle.get_course_contents(course_map.moodle_course_id)

        # Keyed by module id only; section ids live in a different namespace
        # and are never put in here.
        bubble_by_module: dict[int, uuid.UUID] = {}
        for bubble in bubbles:
            bubble_by_module.setdefault(bubble.activity_id, bubble.id)

        activities: list[ActivityRead] = []
        for position, section in enumerate(sections):
            section_number = (
                section.section if section.section is not None else position
            )
            for module in section.modules:
                if not is_bubble_candidate(module):
                    continue
                hidden = is_hidden(section, module)
                if hidden and not include_hidden:
                    continue
                activities.append(
                    ActivityRead(
                        activity_id=module.id,
                        name=module.name,
                        modname=module.modname,
                        url=module.url or "",
                        section_name=section.name,
                        section_number=section_number,
                        hidden=hidden,
                        placed=module.id in bubble_by_module,
                        bubble_id=bubble_by_module.get(module.id),
                    )
                )
        return activities

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
