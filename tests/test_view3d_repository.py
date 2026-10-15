"""The 3D view storage against real Postgres: replace, read, atomicity, cascades
and the per-map uniqueness constraints."""

import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.exceptions import VersionConflictError, View3dInvalidError
from app.models import CourseMap, CourseMapView3dBinding
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.repositories.sqlalchemy.view3d_repository import SqlAlchemyView3dRepository
from app.schemas.bubble import BubbleCreate
from app.schemas.course_map import CourseMapCreate
from app.schemas.view3d import LocalizedText, View3d, View3dBinding


@pytest.fixture
async def observer(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Independent session: only sees committed data."""
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def maps(session: AsyncSession) -> SqlAlchemyCourseMapRepository:
    return SqlAlchemyCourseMapRepository(session)


@pytest.fixture
def bubbles(session: AsyncSession) -> SqlAlchemyBubbleRepository:
    return SqlAlchemyBubbleRepository(session)


@pytest.fixture
def view3d(session: AsyncSession) -> SqlAlchemyView3dRepository:
    return SqlAlchemyView3dRepository(session)


async def make_map(maps: SqlAlchemyCourseMapRepository, course: int = 1) -> uuid.UUID:
    created = await maps.create(
        CourseMapCreate(title="T", moodle_course_id=course, image_url="/u")
    )
    return created.id


async def make_bubbles(
    bubbles: SqlAlchemyBubbleRepository, map_id: uuid.UUID, count: int, course: int = 1
) -> list[uuid.UUID]:
    return [
        (
            await bubbles.create(
                map_id, course, BubbleCreate(activity_id=a + 1, x=0.5, y=0.5)
            )
        ).id
        for a in range(count)
    ]


def binding(
    zone: int,
    bubble: uuid.UUID,
    title: LocalizedText | None = None,
    description: LocalizedText | None = None,
) -> View3dBinding:
    return View3dBinding(
        zone_id=zone, bubble_id=bubble, title=title, description=description
    )


async def revision(observer: AsyncSession, map_id: uuid.UUID) -> int:
    return (
        await observer.execute(select(CourseMap.revision).where(CourseMap.id == map_id))
    ).scalar_one()


async def binding_count(observer: AsyncSession) -> int:
    return (
        await observer.execute(select(func.count()).select_from(CourseMapView3dBinding))
    ).scalar_one()


async def test_a_new_map_has_no_3d_view(
    maps: SqlAlchemyCourseMapRepository, view3d: SqlAlchemyView3dRepository
) -> None:
    map_id = await make_map(maps)

    assert await view3d.get(map_id) is None


async def test_replace_stores_the_scene_and_bindings_ordered_by_zone(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    view3d: SqlAlchemyView3dRepository,
    observer: AsyncSession,
) -> None:
    map_id = await make_map(maps)
    first, second = await make_bubbles(bubbles, map_id, 2)
    title = LocalizedText(es="Hola", en="Hello")
    description = LocalizedText(es="Solo español")

    applied = await view3d.replace(
        map_id,
        View3d(
            scene="region-encuentros",
            bindings=[
                binding(5, first, title=title),
                binding(2, second, description=description),
            ],
        ),
        expected_revision=1,
    )

    assert applied is True
    stored = await SqlAlchemyView3dRepository(observer).get(map_id)
    assert stored is not None and stored.scene == "region-encuentros"
    assert [b.zone_id for b in stored.bindings] == [2, 5]
    assert stored.bindings[0].bubble_id == second
    assert stored.bindings[0].title is None
    assert stored.bindings[0].description == description
    assert stored.bindings[1].title == title
    assert await revision(observer, map_id) == 2  # bumped by the same statement


async def test_replace_swaps_every_binding_and_none_clears_the_view(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    view3d: SqlAlchemyView3dRepository,
    observer: AsyncSession,
) -> None:
    map_id = await make_map(maps)
    first, second = await make_bubbles(bubbles, map_id, 2)
    await view3d.replace(
        map_id,
        View3d(scene="region-encuentros", bindings=[binding(0, first)]),
        expected_revision=1,
    )

    await view3d.replace(
        map_id,
        View3d(scene="region-encuentros", bindings=[binding(0, second)]),
        expected_revision=2,
    )
    swapped = await view3d.get(map_id)
    assert swapped is not None
    assert [b.bubble_id for b in swapped.bindings] == [second]

    await view3d.replace(map_id, None, expected_revision=3)
    assert await view3d.get(map_id) is None
    assert await binding_count(observer) == 0
    assert await revision(observer, map_id) == 4


async def test_a_scene_without_bindings_is_a_3d_view(
    maps: SqlAlchemyCourseMapRepository, view3d: SqlAlchemyView3dRepository
) -> None:
    map_id = await make_map(maps)

    await view3d.replace(
        map_id, View3d(scene="region-encuentros", bindings=[]), expected_revision=1
    )

    assert await view3d.get(map_id) == View3d(scene="region-encuentros", bindings=[])


async def test_a_stale_revision_changes_nothing(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    view3d: SqlAlchemyView3dRepository,
    observer: AsyncSession,
) -> None:
    map_id = await make_map(maps)
    [bubble] = await make_bubbles(bubbles, map_id, 1)

    with pytest.raises(VersionConflictError) as caught:
        await view3d.replace(
            map_id,
            View3d(scene="region-encuentros", bindings=[binding(0, bubble)]),
            expected_revision=7,
        )

    assert caught.value.current_revision == 1
    assert await view3d.get(map_id) is None
    assert await binding_count(observer) == 0


async def test_a_missing_map_is_reported_as_false(
    view3d: SqlAlchemyView3dRepository,
) -> None:
    assert await view3d.replace(uuid.uuid4(), None, expected_revision=1) is False


async def test_one_bad_binding_rolls_everything_back(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    view3d: SqlAlchemyView3dRepository,
    observer: AsyncSession,
) -> None:
    map_id = await make_map(maps)
    first, second = await make_bubbles(bubbles, map_id, 2)
    await view3d.replace(
        map_id,
        View3d(scene="region-encuentros", bindings=[binding(0, first)]),
        expected_revision=1,
    )

    # The second binding names a bubble that no longer exists (it was deleted
    # after the service looked): the foreign key refuses it.
    with pytest.raises(View3dInvalidError) as caught:
        await view3d.replace(
            map_id,
            View3d(
                scene="region-encuentros",
                bindings=[binding(1, second), binding(2, uuid.uuid4())],
            ),
            expected_revision=2,
        )

    assert caught.value.code == "bubble_not_in_map"
    kept = await SqlAlchemyView3dRepository(observer).get(map_id)
    assert kept is not None
    assert [(b.zone_id, b.bubble_id) for b in kept.bindings] == [(0, first)]
    assert await revision(observer, map_id) == 2  # not bumped either


@pytest.mark.parametrize(
    ("bindings", "code"),
    [
        (lambda a, b: [binding(1, a), binding(1, b)], "duplicate_zone"),
        (lambda a, b: [binding(1, a), binding(2, a)], "duplicate_bubble"),
    ],
)
async def test_the_database_is_the_last_line_of_defence_against_duplicates(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    view3d: SqlAlchemyView3dRepository,
    observer: AsyncSession,
    bindings: Callable[[uuid.UUID, uuid.UUID], list[View3dBinding]],
    code: str,
) -> None:
    map_id = await make_map(maps)
    first, second = await make_bubbles(bubbles, map_id, 2)

    with pytest.raises(View3dInvalidError) as caught:
        await view3d.replace(
            map_id,
            View3d(scene="region-encuentros", bindings=bindings(first, second)),
            expected_revision=1,
        )

    assert caught.value.code == code
    assert await view3d.get(map_id) is None
    assert await binding_count(observer) == 0
    assert await revision(observer, map_id) == 1


async def test_constraints_are_per_map(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    view3d: SqlAlchemyView3dRepository,
) -> None:
    one = await make_map(maps, course=1)
    two = await make_map(maps, course=2)
    [in_one] = await make_bubbles(bubbles, one, 1, course=1)
    [in_two] = await make_bubbles(bubbles, two, 1, course=2)

    # Zone 0 on both maps is fine: uniqueness is within one map.
    for map_id, bubble in ((one, in_one), (two, in_two)):
        await view3d.replace(
            map_id,
            View3d(scene="region-encuentros", bindings=[binding(0, bubble)]),
            expected_revision=1,
        )

    assert await view3d.get(one) is not None
    assert await view3d.get(two) is not None


async def test_a_negative_zone_is_refused_by_the_database(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    session: AsyncSession,
) -> None:
    map_id = await make_map(maps)
    [bubble] = await make_bubbles(bubbles, map_id, 1)
    session.add(
        CourseMapView3dBinding(course_map_id=map_id, zone_id=-1, bubble_id=bubble)
    )

    with pytest.raises(IntegrityError, match="ck_course_map_view3d_bindings_zone"):
        await session.commit()
    await session.rollback()


async def test_deleting_a_bubble_removes_its_binding_in_the_same_transaction(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    view3d: SqlAlchemyView3dRepository,
    observer: AsyncSession,
) -> None:
    map_id = await make_map(maps)
    keep, doomed = await make_bubbles(bubbles, map_id, 2)
    await view3d.replace(
        map_id,
        View3d(
            scene="region-encuentros",
            bindings=[binding(0, keep), binding(1, doomed)],
        ),
        expected_revision=1,
    )
    doomed_bubble = await bubbles.get_by_id(doomed)
    assert doomed_bubble is not None

    assert await bubbles.delete(doomed, doomed_bubble.version) is True

    # Seen from another session: the binding is already gone, the scene stays.
    left = await SqlAlchemyView3dRepository(observer).get(map_id)
    assert left is not None and left.scene == "region-encuentros"
    assert [b.bubble_id for b in left.bindings] == [keep]
    assert await binding_count(observer) == 1


async def test_deleting_the_map_removes_its_bindings(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    view3d: SqlAlchemyView3dRepository,
    observer: AsyncSession,
) -> None:
    map_id = await make_map(maps)
    [bubble] = await make_bubbles(bubbles, map_id, 1)
    await view3d.replace(
        map_id,
        View3d(scene="region-encuentros", bindings=[binding(0, bubble)]),
        expected_revision=1,
    )

    assert await maps.delete(map_id) is True

    assert await binding_count(observer) == 0
