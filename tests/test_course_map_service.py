"""Service unit tests against in-memory fakes: no database involved."""

import uuid

import pytest

from app.enums import BubbleIcon, BubbleStatus
from app.exceptions import (
    ActivityAlreadyPlacedError,
    BubbleNotFoundError,
    CourseMapNotFoundError,
)
from app.schemas.bubble import BubbleCreate, BubbleUpdate
from app.schemas.course_map import CourseMapCreate, CourseMapUpdate
from app.services.course_map_service import CourseMapService
from tests.fakes import (
    InMemoryBubbleRepository,
    InMemoryCourseMapRepository,
    InMemoryMoodleClient,
    InMemorySkinRepository,
)


@pytest.fixture
def maps() -> InMemoryCourseMapRepository:
    return InMemoryCourseMapRepository()


@pytest.fixture
def bubbles(maps: InMemoryCourseMapRepository) -> InMemoryBubbleRepository:
    return InMemoryBubbleRepository(maps)


@pytest.fixture
def moodle() -> InMemoryMoodleClient:
    return InMemoryMoodleClient()


@pytest.fixture
def skins() -> InMemorySkinRepository:
    return InMemorySkinRepository()


@pytest.fixture
def service(
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    moodle: InMemoryMoodleClient,
    skins: InMemorySkinRepository,
) -> CourseMapService:
    # Passing the fakes here is the Protocol check: mypy verifies they match.
    return CourseMapService(maps, bubbles, moodle, skins)


async def _map_id(service: CourseMapService, course_id: int = 1) -> uuid.UUID:
    created = await service.create_course_map(
        CourseMapCreate(title="T", moodle_course_id=course_id, image_url="/u")
    )
    return created.id


async def test_a_course_can_have_several_maps_in_order(
    service: CourseMapService,
) -> None:
    first, second = await _map_id(service, 7), await _map_id(service, 7)

    listed = await service.list_course_maps(
        moodle_course_id=7, q=None, limit=10, offset=0
    )

    assert [(m.id, m.position) for m in listed.items] == [(first, 0), (second, 1)]


async def test_list_course_maps_wraps_the_page_and_counts_bubbles(
    service: CourseMapService,
) -> None:
    first, second = await _map_id(service, 1), await _map_id(service, 2)
    await service.add_bubble(first, BubbleCreate(activity_id=1, x=0, y=0))
    await service.add_bubble(first, BubbleCreate(activity_id=2, x=0, y=0))

    page = await service.list_course_maps(
        moodle_course_id=None, q=None, limit=1, offset=0
    )

    assert (page.total, page.limit, page.offset) == (2, 1, 0)
    assert [m.id for m in page.items] == [second]
    everything = await service.list_course_maps(
        moodle_course_id=1, q="  t ", limit=24, offset=0
    )
    assert [(m.id, m.bubble_count) for m in everything.items] == [(first, 2)]


async def test_update_course_map_returns_map_with_bubbles(
    service: CourseMapService,
) -> None:
    map_id = await _map_id(service)
    await service.add_bubble(map_id, BubbleCreate(activity_id=1, x=0, y=0))

    updated = await service.update_course_map(map_id, CourseMapUpdate(title="Renamed"))

    assert updated.title == "Renamed" and updated.image_url == "/u"
    assert len(updated.bubbles) == 1


async def test_update_and_delete_missing_map_raise(service: CourseMapService) -> None:
    with pytest.raises(CourseMapNotFoundError):
        await service.update_course_map(uuid.uuid4(), CourseMapUpdate(title="x"))
    with pytest.raises(CourseMapNotFoundError):
        await service.delete_course_map(uuid.uuid4())


async def test_delete_course_map(service: CourseMapService) -> None:
    map_id = await _map_id(service)
    await service.delete_course_map(map_id)
    with pytest.raises(CourseMapNotFoundError):
        await service.get_course_map(map_id)


async def test_get_course_map_includes_its_bubbles_only(
    service: CourseMapService,
) -> None:
    first, second = await _map_id(service, 1), await _map_id(service, 2)
    await service.add_bubble(first, BubbleCreate(activity_id=1, x=0.1, y=0.1))
    await service.add_bubble(second, BubbleCreate(activity_id=2, x=0.2, y=0.2))

    detail = await service.get_course_map(first)
    assert detail.id == first
    assert [b.activity_id for b in detail.bubbles] == [1]


async def test_add_bubble_twice_for_the_same_activity_raises(
    service: CourseMapService,
) -> None:
    map_id = await _map_id(service)
    await service.add_bubble(map_id, BubbleCreate(activity_id=1, x=0, y=0))
    with pytest.raises(ActivityAlreadyPlacedError):
        await service.add_bubble(map_id, BubbleCreate(activity_id=1, x=0.5, y=0.5))
    other = await _map_id(service, 2)  # another map may reuse the activity
    await service.add_bubble(other, BubbleCreate(activity_id=1, x=0, y=0))


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
