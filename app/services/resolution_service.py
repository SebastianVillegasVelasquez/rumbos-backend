import logging
import uuid
from dataclasses import dataclass

from app.enums import Availability, MoodleStatus
from app.exceptions import CourseMapNotFoundError
from app.moodle.exceptions import MoodleCourseNotFoundError, MoodleError
from app.moodle.protocols import CourseContentsProvider
from app.moodle.schemas import MoodleModule, MoodleSection
from app.repositories.protocols import BubbleRepository, CourseMapRepository
from app.schemas.bubble import BubbleRead
from app.schemas.resolved import ResolvedActivity, ResolvedBubble, ResolvedMap
from app.services.course_map_service import (
    is_bubble_candidate,
    is_hidden,
    section_number,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Found:
    section: MoodleSection
    number: int
    module: MoodleModule


class MapResolutionService:
    """Resolves each bubble of a map to what a learner would find in Moodle.

    The visibility rules (`is_hidden`, `is_bubble_candidate`) live in
    `course_map_service` and are reused, not copied. Moodle failures never
    propagate: they degrade the answer (`unknown` / `unavailable`) because the
    database part of it is still valid. The cache decorator behind
    `CourseContentsProvider` already logs those failures (ERROR for auth or
    configuration problems), so they are not logged a second time here.
    """

    def __init__(
        self,
        course_maps: CourseMapRepository,
        bubbles: BubbleRepository,
        contents: CourseContentsProvider,
    ) -> None:
        self._course_maps = course_maps
        self._bubbles = bubbles
        self._contents = contents

    async def resolve(
        self, course_map_id: uuid.UUID, *, include_hidden: bool = False
    ) -> ResolvedMap:
        course_map = await self._course_maps.get_by_id(course_map_id)
        if course_map is None:
            raise CourseMapNotFoundError(course_map_id)
        bubbles = await self._bubbles.list_by_course_map(course_map_id)

        sections, moodle_status = await self._load(course_map.moodle_course_id)
        if sections is None:
            unknown = [
                ResolvedBubble(
                    bubble_id=b.id, availability=Availability.UNKNOWN, activity=None
                )
                for b in bubbles
            ]
            return ResolvedMap(moodle_status=moodle_status, bubbles=unknown)

        # Keyed by module id (cmid) only. Section ids share the number space
        # and collide with module ids (section 25 vs quiz module 25), so they
        # are never put in here.
        modules: dict[int, _Found] = {}
        for position, section in enumerate(sections):
            number = section_number(section, position)
            for module in section.modules:
                modules.setdefault(module.id, _Found(section, number, module))

        return ResolvedMap(
            moodle_status=moodle_status,
            bubbles=[self._resolve_bubble(b, modules, include_hidden) for b in bubbles],
        )

    async def _load(
        self, moodle_course_id: int
    ) -> tuple[list[MoodleSection] | None, MoodleStatus]:
        """Sections (None if Moodle can't be asked) and the status to report."""
        try:
            result = await self._contents.fetch_course_contents(moodle_course_id)
        except MoodleCourseNotFoundError:
            # Moodle answered: the course is gone, so every activity is too.
            return [], MoodleStatus.LIVE
        except MoodleError:
            return None, MoodleStatus.UNAVAILABLE
        status = MoodleStatus.CACHED if result.stale else MoodleStatus.LIVE
        return result.sections, status

    @staticmethod
    def _resolve_bubble(
        bubble: BubbleRead, modules: dict[int, _Found], include_hidden: bool
    ) -> ResolvedBubble:
        found = modules.get(bubble.activity_id)
        if found is None or not is_bubble_candidate(found.module):
            return ResolvedBubble(
                bubble_id=bubble.id, availability=Availability.MISSING, activity=None
            )
        module = found.module
        activity = ResolvedActivity(
            activity_id=module.id,
            name=module.name,
            modname=module.modname,
            section_name=found.section.name,
            section_number=found.number,
            url=module.url or "",
        )
        if is_hidden(found.section, module):
            # What a hidden activity is called is not for learners to see.
            return ResolvedBubble(
                bubble_id=bubble.id,
                availability=Availability.HIDDEN,
                activity=activity if include_hidden else None,
            )
        return ResolvedBubble(
            bubble_id=bubble.id, availability=Availability.AVAILABLE, activity=activity
        )
