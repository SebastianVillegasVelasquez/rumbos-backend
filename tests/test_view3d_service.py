"""Content checks and the service over in-memory fakes: no database."""

import uuid

import pytest

from app.exceptions import (
    CourseMapNotFoundError,
    VersionConflictError,
    View3dInvalidError,
)
from app.schemas.bubble import BubbleCreate
from app.schemas.course_map import CourseMapCreate
from app.schemas.view3d import (
    View3d,
    View3dBinding,
    View3dBindingUpdate,
    View3dUpdate,
)
from app.services.course_map_service import CourseMapService
from app.view3d.validation import check_view3d
from tests.fakes import (
    InMemoryAssetRepository,
    InMemoryBubbleRepository,
    InMemoryCourseMapRepository,
    InMemoryMoodleClient,
    InMemoryPublicationRepository,
    InMemorySkinRepository,
    InMemoryView3dRepository,
)

SCENE = "region-encuentros"


def bound(zone: int, bubble: uuid.UUID) -> View3dBinding:
    return View3dBinding(zone_id=zone, bubble_id=bubble)


def code_of(view3d: View3d, bubbles: list[uuid.UUID]) -> str:
    with pytest.raises(View3dInvalidError) as caught:
        check_view3d(view3d, bubbles)
    return caught.value.code


def test_a_valid_view_passes() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    check_view3d(View3d(scene=SCENE, bindings=[bound(0, a), bound(8, b)]), [a, b])
    check_view3d(View3d(scene=SCENE, bindings=[]), [])


def test_checks_run_in_the_order_of_the_contract() -> None:
    a, b, ghost = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    everything_wrong = [bound(99, a), bound(99, a), bound(1, ghost)]

    assert code_of(View3d(scene="nope", bindings=everything_wrong), [a]) == (
        "scene_unknown"
    )
    assert code_of(View3d(scene=SCENE, bindings=everything_wrong), [a]) == (
        "zone_out_of_range"
    )
    assert code_of(
        View3d(scene=SCENE, bindings=[bound(1, a), bound(1, a), bound(2, ghost)]), [a]
    ) == ("duplicate_zone")
    assert code_of(
        View3d(scene=SCENE, bindings=[bound(1, a), bound(2, a), bound(3, ghost)]), [a]
    ) == ("duplicate_bubble")
    assert code_of(View3d(scene=SCENE, bindings=[bound(1, a), bound(2, b)]), [a]) == (
        "bubble_not_in_map"
    )


@pytest.fixture
def maps() -> InMemoryCourseMapRepository:
    return InMemoryCourseMapRepository()


@pytest.fixture
def bubbles(maps: InMemoryCourseMapRepository) -> InMemoryBubbleRepository:
    return InMemoryBubbleRepository(maps)


@pytest.fixture
def view3d(
    maps: InMemoryCourseMapRepository, bubbles: InMemoryBubbleRepository
) -> InMemoryView3dRepository:
    return InMemoryView3dRepository(maps, bubbles)


@pytest.fixture
def service(
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    view3d: InMemoryView3dRepository,
) -> CourseMapService:
    return CourseMapService(
        maps,
        bubbles,
        InMemoryMoodleClient(),
        InMemorySkinRepository(),
        InMemoryAssetRepository(),
        InMemoryPublicationRepository(),
        view3d,
    )


async def map_with_bubbles(
    service: CourseMapService, count: int
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    created = await service.create_course_map(
        CourseMapCreate(title="T", moodle_course_id=1, image_url="/u")
    )
    ids = [
        (
            await service.add_bubble(
                created.id, BubbleCreate(activity_id=n + 1, x=0.5, y=0.5)
            )
        ).id
        for n in range(count)
    ]
    return created.id, ids


def update(*pairs: tuple[int, uuid.UUID]) -> View3dUpdate:
    return View3dUpdate(
        scene=SCENE,
        bindings=[View3dBindingUpdate(zone_id=z, bubble_id=b) for z, b in pairs],
    )


async def test_an_invalid_view_writes_nothing(
    service: CourseMapService, view3d: InMemoryView3dRepository
) -> None:
    map_id, [bubble] = await map_with_bubbles(service, 1)

    with pytest.raises(View3dInvalidError):
        await service.update_view3d(map_id, update((0, bubble), (0, bubble)), 1)

    assert view3d.writes == 0
    assert (await service.get_course_map(map_id)).revision == 1


async def test_update_returns_the_map_with_its_view_and_bumps_the_revision(
    service: CourseMapService,
) -> None:
    map_id, [a, b] = await map_with_bubbles(service, 2)

    result = await service.update_view3d(map_id, update((5, b), (1, a)), 1)

    assert result.revision == 2
    assert result.view3d is not None
    assert [x.zone_id for x in result.view3d.bindings] == [1, 5]
    cleared = await service.update_view3d(map_id, None, 2)
    assert cleared.view3d is None and cleared.revision == 3


async def test_a_stale_revision_and_a_missing_map(service: CourseMapService) -> None:
    map_id, [bubble] = await map_with_bubbles(service, 1)

    with pytest.raises(VersionConflictError):
        await service.update_view3d(map_id, update((0, bubble)), 9)
    with pytest.raises(CourseMapNotFoundError):
        await service.update_view3d(uuid.uuid4(), None, 1)


async def test_a_removed_bubble_leaves_no_binding_behind(
    service: CourseMapService,
) -> None:
    map_id, [keep, doomed] = await map_with_bubbles(service, 2)
    await service.update_view3d(map_id, update((0, keep), (1, doomed)), 1)

    await service.remove_bubble(map_id, doomed, 1)

    view = (await service.get_course_map(map_id)).view3d
    assert view is not None
    assert [b.bubble_id for b in view.bindings] == [keep]
