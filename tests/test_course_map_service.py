"""Service unit tests against in-memory fakes: no database involved."""

import uuid

import pytest

from app.enums import BubbleIcon, BubbleStatus
from app.exceptions import (
    BubbleNotFoundError,
    CourseMapAlreadyExistsError,
    CourseMapNotFoundError,
)
from app.schemas.bubble import BubbleCreate, BubbleUpdate
from app.schemas.course_map import CourseMapCreate
from app.services.course_map_service import CourseMapService
from tests.fakes import InMemoryBubbleRepository, InMemoryCourseMapRepository


@pytest.fixture
def maps() -> InMemoryCourseMapRepository:
    return InMemoryCourseMapRepository()


@pytest.fixture
def bubbles(maps: InMemoryCourseMapRepository) -> InMemoryBubbleRepository:
    return InMemoryBubbleRepository(maps)


@pytest.fixture
def service(
    maps: InMemoryCourseMapRepository, bubbles: InMemoryBubbleRepository
) -> CourseMapService:
    # Passing the fakes here is the Protocol check: mypy verifies they match.
    return CourseMapService(maps, bubbles)


async def _map_id(service: CourseMapService, course_id: int = 1) -> uuid.UUID:
    created = await service.create_course_map(
        CourseMapCreate(moodle_course_id=course_id, image_url="u")
    )
    return created.id


async def test_create_course_map_rejects_second_map_for_same_course(
    service: CourseMapService,
) -> None:
    await _map_id(service, 7)
    with pytest.raises(CourseMapAlreadyExistsError):
        await _map_id(service, 7)


async def test_get_course_map_includes_its_bubbles_only(
    service: CourseMapService,
) -> None:
    first, second = await _map_id(service, 1), await _map_id(service, 2)
    await service.add_bubble(first, BubbleCreate(activity_id=1, x=0.1, y=0.1))
    await service.add_bubble(second, BubbleCreate(activity_id=2, x=0.2, y=0.2))

    detail = await service.get_course_map(first)
    assert detail.id == first
    assert [b.activity_id for b in detail.bubbles] == [1]


async def test_get_missing_course_map_raises(service: CourseMapService) -> None:
    with pytest.raises(CourseMapNotFoundError):
        await service.get_course_map(uuid.uuid4())


async def test_add_bubble_to_missing_map_raises(service: CourseMapService) -> None:
    with pytest.raises(CourseMapNotFoundError):
        await service.add_bubble(uuid.uuid4(), BubbleCreate(activity_id=1, x=0, y=0))


async def test_partial_update_only_touches_sent_fields(
    service: CourseMapService, bubbles: InMemoryBubbleRepository
) -> None:
    map_id = await _map_id(service)
    bubble = await service.add_bubble(
        map_id, BubbleCreate(activity_id=1, x=0.1, y=0.2, icon=BubbleIcon.FLAG)
    )
    writes_before = bubbles.writes

    moved = await service.update_bubble(
        map_id, bubble.id, BubbleUpdate.model_validate({"x": 0.9, "y": 0.8})
    )
    assert (moved.x, moved.y) == (0.9, 0.8)
    assert moved.icon is BubbleIcon.FLAG  # icon was not resent, so untouched
    assert moved.status is BubbleStatus.LOCKED
    assert bubbles.writes - writes_before == 1  # only the position write ran


async def test_update_can_clear_icon_and_change_status(
    service: CourseMapService,
) -> None:
    map_id = await _map_id(service)
    bubble = await service.add_bubble(
        map_id, BubbleCreate(activity_id=1, x=0, y=0, icon=BubbleIcon.STAR)
    )
    updated = await service.update_bubble(
        map_id,
        bubble.id,
        BubbleUpdate.model_validate({"icon": None, "status": "complete"}),
    )
    assert updated.icon is None
    assert updated.status is BubbleStatus.COMPLETE


async def test_bubble_addressed_through_wrong_map_is_not_found(
    service: CourseMapService,
) -> None:
    owner, other = await _map_id(service, 1), await _map_id(service, 2)
    bubble = await service.add_bubble(owner, BubbleCreate(activity_id=1, x=0, y=0))

    with pytest.raises(BubbleNotFoundError):
        await service.update_bubble(
            other, bubble.id, BubbleUpdate.model_validate({"status": "complete"})
        )
    with pytest.raises(BubbleNotFoundError):
        await service.remove_bubble(other, bubble.id)
    # Still intact under its real map.
    assert len((await service.get_course_map(owner)).bubbles) == 1


async def test_remove_bubble(service: CourseMapService) -> None:
    map_id = await _map_id(service)
    bubble = await service.add_bubble(map_id, BubbleCreate(activity_id=1, x=0, y=0))
    await service.remove_bubble(map_id, bubble.id)
    assert (await service.get_course_map(map_id)).bubbles == []
    with pytest.raises(BubbleNotFoundError):
        await service.remove_bubble(map_id, bubble.id)
