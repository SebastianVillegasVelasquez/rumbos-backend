"""`CourseMapService.list_activities` against in-memory fakes: no HTTP, no database."""

import uuid
from typing import Any

import pytest

from app.exceptions import CourseMapNotFoundError
from app.moodle.exceptions import (
    MoodleAuthError,
    MoodleCourseNotFoundError,
    MoodleUnavailableError,
)
from app.moodle.schemas import MoodleModule, MoodleSection
from app.schemas.bubble import BubbleCreate
from app.schemas.course_map import CourseMapCreate
from app.services.course_map_service import CourseMapService, is_bubble_candidate
from tests.fakes import (
    InMemoryBubbleRepository,
    InMemoryCourseMapRepository,
    InMemoryMoodleClient,
)


@pytest.fixture
def moodle() -> InMemoryMoodleClient:
    return InMemoryMoodleClient()


@pytest.fixture
def service(moodle: InMemoryMoodleClient) -> CourseMapService:
    maps = InMemoryCourseMapRepository()
    return CourseMapService(maps, InMemoryBubbleRepository(maps), moodle)


def _module(id: int, modname: str = "quiz", **extra: Any) -> MoodleModule:
    return MoodleModule(id=id, name=f"{modname} {id}", modname=modname, **extra)


async def _map_id(service: CourseMapService, course_id: int) -> uuid.UUID:
    created = await service.create_course_map(
        CourseMapCreate(moodle_course_id=course_id, image_url="u")
    )
    return created.id


async def test_flattens_sections_in_course_order(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _map_id(service, 7)
    moodle.courses[7] = [
        MoodleSection(id=1, name="General", modules=[_module(10, "forum")]),
        MoodleSection(id=2, name="Unit 1", modules=[_module(20), _module(21, "url")]),
        MoodleSection(id=3, name="Empty"),
    ]

    activities = await service.list_activities(map_id)

    assert [(a.activity_id, a.modname, a.section_name) for a in activities] == [
        (10, "forum", "General"),
        (20, "quiz", "Unit 1"),
        (21, "url", "Unit 1"),
    ]
    assert activities[0].name == "forum 10"


async def test_excludes_labels_and_hidden_modules(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _map_id(service, 7)
    moodle.courses[7] = [
        MoodleSection(
            id=1,
            name="S",
            modules=[
                _module(1, "label"),
                _module(2, visible=0),
                _module(3, uservisible=False),
                _module(4, visible=1, uservisible=True),
                _module(5),  # flags missing: treated as visible
            ],
        )
    ]

    activities = await service.list_activities(map_id)

    assert [a.activity_id for a in activities] == [4, 5]


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        (_module(1), True),
        (_module(1, "label"), False),
        (_module(1, visible=0), False),
        (_module(1, uservisible=False), False),
        (_module(1, visible=1, uservisible=True), True),
    ],
)
def test_is_bubble_candidate(module: MoodleModule, expected: bool) -> None:
    assert is_bubble_candidate(module) is expected


async def test_cross_references_bubbles_of_this_map_only(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _map_id(service, 7)
    other_map = await _map_id(service, 8)
    placed = await service.add_bubble(map_id, BubbleCreate(activity_id=20, x=0, y=0))
    # A bubble on another map must not mark the activity as placed here.
    await service.add_bubble(other_map, BubbleCreate(activity_id=21, x=0, y=0))
    moodle.courses[7] = [
        MoodleSection(id=1, name="S", modules=[_module(20), _module(21)])
    ]

    placed_activity, free_activity = await service.list_activities(map_id)

    assert placed_activity.placed is True
    assert placed_activity.bubble_id == placed.id
    assert free_activity.placed is False
    assert free_activity.bubble_id is None


async def test_asks_moodle_for_the_maps_own_course(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _map_id(service, 7)
    moodle.courses[7] = [MoodleSection(id=1, name="S", modules=[_module(1)])]
    moodle.courses[8] = [MoodleSection(id=2, name="Wrong", modules=[_module(2)])]

    activities = await service.list_activities(map_id)

    assert [a.activity_id for a in activities] == [1]


async def test_unknown_map_does_not_call_moodle(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    moodle.error = MoodleUnavailableError("would fail if called")
    with pytest.raises(CourseMapNotFoundError):
        await service.list_activities(uuid.uuid4())


async def test_moodle_errors_propagate_unchanged(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _map_id(service, 7)
    with pytest.raises(MoodleCourseNotFoundError):  # course absent in the fake
        await service.list_activities(map_id)
    moodle.error = MoodleAuthError("nope")
    with pytest.raises(MoodleAuthError):
        await service.list_activities(map_id)
