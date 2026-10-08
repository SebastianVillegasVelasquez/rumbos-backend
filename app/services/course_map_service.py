import uuid
from collections import Counter

from app.exceptions import (
    BubbleNotFoundError,
    CourseMapNotFoundError,
    DuplicateSkinRuleError,
    OrderMismatchError,
    SkinReferenceNotFoundError,
)
from app.moodle.protocols import MoodleClient
from app.moodle.schemas import MoodleModule, MoodleSection
from app.repositories.protocols import (
    BubbleRepository,
    CourseMapRepository,
    SkinRepository,
)
from app.schemas.activity import ActivityRead
from app.schemas.bubble import (
    BubbleCreate,
    BubbleOrdered,
    BubbleOrderUpdate,
    BubbleRead,
    BubbleUpdate,
)
from app.schemas.course_map import (
    AppearanceUpdate,
    CourseMapBase,
    CourseMapCreate,
    CourseMapList,
    CourseMapOrdered,
    CourseMapOrderUpdate,
    CourseMapRead,
    CourseMapUpdate,
)


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


def section_number(section: MoodleSection, position: int) -> int:
    """The section's number in the course; its list position if Moodle omits it."""
    return section.section if section.section is not None else position


def check_exact_order(existing: list[uuid.UUID], requested: list[uuid.UUID]) -> None:
    """Raises `OrderMismatchError` unless `requested` is `existing` reordered:
    same ids, none missing, none unknown, none repeated."""
    counts = Counter(requested)
    known = set(existing)
    duplicated = [i for i, n in counts.items() if n > 1]
    missing = [i for i in existing if i not in counts]
    unexpected = [i for i in counts if i not in known]
    if missing or unexpected or duplicated:
        raise OrderMismatchError(missing, unexpected, duplicated)


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
        skins: SkinRepository,
    ) -> None:
        self._course_maps = course_maps
        self._bubbles = bubbles
        self._moodle = moodle
        self._skins = skins

    async def create_course_map(self, data: CourseMapCreate) -> CourseMapRead:
        # The repository appends the map to the course and enforces one map
        # per section (`SectionAlreadyMappedError`).
        created = await self._course_maps.create(data)
        return CourseMapRead(**created.model_dump(), skin_rules=[], bubbles=[])

    async def reorder_course_maps(self, data: CourseMapOrderUpdate) -> CourseMapOrdered:
        """Sets the order of a course's maps; `map_ids` must be exactly its maps."""
        existing = await self._course_maps.list_for_course(data.moodle_course_id)
        check_exact_order([m.id for m in existing], data.map_ids)
        await self._course_maps.set_order(data.moodle_course_id, data.map_ids)
        return CourseMapOrdered(
            items=await self._course_maps.list_for_course(data.moodle_course_id)
        )

    async def list_course_maps(
        self,
        *,
        moodle_course_id: int | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> CourseMapList:
        items, total = await self._course_maps.list(
            moodle_course_id, (q or "").strip() or None, limit, offset
        )
        return CourseMapList(items=items, total=total, limit=limit, offset=offset)

    async def get_course_map(self, course_map_id: uuid.UUID) -> CourseMapRead:
        course_map = await self._require_course_map(course_map_id)
        return await self._read(course_map)

    async def update_course_map(
        self, course_map_id: uuid.UUID, data: CourseMapUpdate
    ) -> CourseMapRead:
        """Applies only the fields present in `data` (partial update)."""
        updated = await self._course_maps.update(course_map_id, data)
        if updated is None:
            raise CourseMapNotFoundError(course_map_id)
        return await self._read(updated)

    async def update_appearance(
        self, course_map_id: uuid.UUID, data: AppearanceUpdate
    ) -> CourseMapRead:
        """Replaces settings, default skin and skin rules, all or nothing.

        Every skin referenced must exist and no activity type may appear in two
        rules. Nothing is written unless all of it is valid.
        """
        await self._require_course_map(course_map_id)
        counts = Counter(r.modname for r in data.skin_rules)
        duplicated = sorted(modname for modname, n in counts.items() if n > 1)
        if duplicated:
            raise DuplicateSkinRuleError(duplicated)

        wanted = {r.skin_id for r in data.skin_rules}
        if data.default_skin_id is not None:
            wanted.add(data.default_skin_id)
        missing = wanted - await self._skins.existing_ids(wanted)
        if missing:
            raise SkinReferenceNotFoundError(sorted(missing))

        applied = await self._course_maps.replace_appearance(
            course_map_id, data.settings, data.default_skin_id, data.skin_rules
        )
        if not applied:  # deleted between the lookup and the write
            raise CourseMapNotFoundError(course_map_id)
        return await self.get_course_map(course_map_id)

    async def delete_course_map(self, course_map_id: uuid.UUID) -> None:
        """Deletes the map; its bubbles go with it (ON DELETE CASCADE)."""
        if not await self._course_maps.delete(course_map_id):
            raise CourseMapNotFoundError(course_map_id)

    async def add_bubble(
        self, course_map_id: uuid.UUID, data: BubbleCreate
    ) -> BubbleRead:
        course_map = await self._require_course_map(course_map_id)
        # The bubble carries its map's Moodle course (immutable), which is what
        # makes an activity unique per course.
        return await self._bubbles.create(
            course_map_id, course_map.moodle_course_id, data
        )

    async def update_bubble(
        self, course_map_id: uuid.UUID, bubble_id: uuid.UUID, data: BubbleUpdate
    ) -> BubbleRead:
        """Applies only the fields present in `data` (partial update)."""
        await self._require_bubble(course_map_id, bubble_id)
        if data.skin_id is not None and not await self._skins.existing_ids(
            {data.skin_id}
        ):
            raise SkinReferenceNotFoundError([data.skin_id])
        updated = await self._bubbles.update(bubble_id, data)
        if updated is None:  # deleted between the lookup and the write
            raise BubbleNotFoundError(bubble_id)
        return updated

    async def reorder_bubbles(
        self, course_map_id: uuid.UUID, data: BubbleOrderUpdate
    ) -> BubbleOrdered:
        """Sets the guided path: `bubble_ids` must be exactly the map's bubbles."""
        await self._require_course_map(course_map_id)
        existing = await self._bubbles.list_by_course_map(course_map_id)
        check_exact_order([b.id for b in existing], data.bubble_ids)
        await self._bubbles.set_order(course_map_id, data.bubble_ids)
        return BubbleOrdered(
            bubbles=await self._bubbles.list_by_course_map(course_map_id)
        )

    async def remove_bubble(
        self, course_map_id: uuid.UUID, bubble_id: uuid.UUID
    ) -> None:
        await self._require_bubble(course_map_id, bubble_id)
        if not await self._bubbles.delete(bubble_id):
            raise BubbleNotFoundError(bubble_id)

    async def list_activities(
        self,
        course_map_id: uuid.UUID,
        *,
        include_hidden: bool = False,
        only_section: bool = False,
    ) -> list[ActivityRead]:
        """The map's Moodle course activities, flattened in Moodle's order.

        Order is section number, then position in the section's `modules`
        list. Each activity says whether a bubble on ANY map of the course
        already references it (by module id), which map, and whether learners
        would not see it. Hidden activities are left out unless
        `include_hidden`. With `only_section`, only the activities of the
        map's Moodle section are listed (ignored for a map without one).
        Raises the `Moodle*Error`s from the client unchanged.
        """
        course_map = await self._require_course_map(course_map_id)
        bubbles = await self._bubbles.list_by_course(course_map.moodle_course_id)
        sections = await self._moodle.get_course_contents(course_map.moodle_course_id)

        # Keyed by module id only; section ids live in a different namespace
        # and are never put in here.
        bubble_by_module: dict[int, BubbleRead] = {}
        for bubble in bubbles:
            bubble_by_module.setdefault(bubble.activity_id, bubble)
        section_filter = course_map.moodle_section_id if only_section else None

        activities: list[ActivityRead] = []
        for position, section in enumerate(sections):
            if section_filter is not None and section.id != section_filter:
                continue
            number = section_number(section, position)
            for module in section.modules:
                if not is_bubble_candidate(module):
                    continue
                hidden = is_hidden(section, module)
                if hidden and not include_hidden:
                    continue
                placed = bubble_by_module.get(module.id)
                activities.append(
                    ActivityRead(
                        activity_id=module.id,
                        name=module.name,
                        modname=module.modname,
                        url=module.url or "",
                        section_id=section.id,
                        section_name=section.name,
                        section_number=number,
                        hidden=hidden,
                        placed=placed is not None,
                        bubble_id=placed.id if placed else None,
                        placed_in_map_id=placed.course_map_id if placed else None,
                    )
                )
        return activities

    async def _read(self, course_map: CourseMapBase) -> CourseMapRead:
        return CourseMapRead(
            **course_map.model_dump(),
            skin_rules=await self._course_maps.list_skin_rules(course_map.id),
            bubbles=await self._bubbles.list_by_course_map(course_map.id),
        )

    async def _require_course_map(self, course_map_id: uuid.UUID) -> CourseMapBase:
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
