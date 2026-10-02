"""Integration tests: repositories against real Postgres.

A second, independent session is used to observe what was really committed.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.enums import BubbleIcon, BubbleStatus
from app.exceptions import CourseMapAlreadyExistsError, CourseMapNotFoundError
from app.models import Bubble, CourseMap
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.schemas.bubble import BubbleCreate
from app.schemas.course_map import CourseMapCreate


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


async def _count(s: AsyncSession, model: type[CourseMap] | type[Bubble]) -> int:
    return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _make_map(
    maps: SqlAlchemyCourseMapRepository, course_id: int = 1
) -> uuid.UUID:
    return (
        await maps.create(CourseMapCreate(moodle_course_id=course_id, image_url="u"))
    ).id


def _spy_commits(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = session.commit

    async def spy() -> None:
        calls.append(1)
        await real()

    monkeypatch.setattr(session, "commit", spy)
    return calls


# --- course maps -----------------------------------------------------------


async def test_create_commits_and_is_visible_to_other_sessions(
    maps: SqlAlchemyCourseMapRepository, observer: AsyncSession
) -> None:
    created = await maps.create(CourseMapCreate(moodle_course_id=9, image_url="img"))
    assert created.id.version == 7
    stored = await observer.get(CourseMap, created.id)
    assert stored is not None and stored.moodle_course_id == 9


async def test_duplicate_moodle_course_raises_and_rolls_back(
    maps: SqlAlchemyCourseMapRepository,
    session: AsyncSession,
    observer: AsyncSession,
) -> None:
    await maps.create(CourseMapCreate(moodle_course_id=3, image_url="a"))
    with pytest.raises(CourseMapAlreadyExistsError):
        await maps.create(CourseMapCreate(moodle_course_id=3, image_url="b"))
    # The session is usable again (rolled back) and nothing extra persisted.
    assert await _count(session, CourseMap) == 1
    assert await _count(observer, CourseMap) == 1


async def test_get_by_id_and_by_moodle_course_id(
    maps: SqlAlchemyCourseMapRepository,
) -> None:
    map_id = await _make_map(maps, 4)
    by_id = await maps.get_by_id(map_id)
    by_course = await maps.get_by_moodle_course_id(4)
    assert by_id is not None and by_course is not None and by_id.id == by_course.id
    assert await maps.get_by_id(uuid.uuid4()) is None
    assert await maps.get_by_moodle_course_id(404) is None


async def test_delete_map_cascades_and_commits(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    await bubbles.create(map_id, BubbleCreate(activity_id=1, x=0.1, y=0.1))
    assert await maps.delete(map_id) is True
    assert await _count(observer, CourseMap) == 0
    assert await _count(observer, Bubble) == 0
    assert await maps.delete(map_id) is False


# --- bubbles ---------------------------------------------------------------


async def test_bubble_create_and_list_in_creation_order(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    for activity in (10, 20, 30):
        await bubbles.create(map_id, BubbleCreate(activity_id=activity, x=0.5, y=0.5))
    listed = await bubbles.list_by_course_map(map_id)
    assert [b.activity_id for b in listed] == [10, 20, 30]
    assert await _count(observer, Bubble) == 3
    assert await bubbles.list_by_course_map(uuid.uuid4()) == []


async def test_bubble_create_for_missing_map_raises_and_rolls_back(
    bubbles: SqlAlchemyBubbleRepository, session: AsyncSession
) -> None:
    with pytest.raises(CourseMapNotFoundError):
        await bubbles.create(uuid.uuid4(), BubbleCreate(activity_id=1, x=0, y=0))
    assert await _count(session, Bubble) == 0


async def test_bubble_updates_commit_and_touch_updated_at(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    bubble = await bubbles.create(map_id, BubbleCreate(activity_id=1, x=0.1, y=0.2))

    moved = await bubbles.update_position(bubble.id, 0.7, 0.8)
    assert moved is not None and (moved.x, moved.y) == (0.7, 0.8)
    assert moved.updated_at > bubble.updated_at
    iconed = await bubbles.update_icon(bubble.id, BubbleIcon.STAR)
    assert iconed is not None and iconed.icon is BubbleIcon.STAR
    # Moving earlier did not clobber anything else, and icon can be cleared.
    cleared = await bubbles.update_icon(bubble.id, None)
    assert cleared is not None and cleared.icon is None
    status = await bubbles.update_status(bubble.id, BubbleStatus.COMPLETE)
    assert status is not None and status.status is BubbleStatus.COMPLETE

    stored = await observer.get(Bubble, bubble.id)
    assert stored is not None
    assert (stored.x, stored.y, stored.icon, stored.status) == (
        0.7,
        0.8,
        None,
        BubbleStatus.COMPLETE,
    )


async def test_bubble_updates_on_missing_bubble_return_none(
    bubbles: SqlAlchemyBubbleRepository,
) -> None:
    missing = uuid.uuid4()
    assert await bubbles.update_position(missing, 0, 0) is None
    assert await bubbles.update_icon(missing, None) is None
    assert await bubbles.update_status(missing, BubbleStatus.LOCKED) is None
    assert await bubbles.delete(missing) is False


async def test_bubble_delete_commits(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    bubble = await bubbles.create(map_id, BubbleCreate(activity_id=1, x=0, y=0))
    assert await bubbles.delete(bubble.id) is True
    assert await _count(observer, Bubble) == 0
    assert await bubbles.get_by_id(bubble.id) is None


# --- commit / rollback placement -------------------------------------------


async def test_read_methods_never_commit(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    map_id = await _make_map(maps)
    bubble = await bubbles.create(map_id, BubbleCreate(activity_id=1, x=0, y=0))
    calls = _spy_commits(session, monkeypatch)

    await maps.get_by_id(map_id)
    await maps.get_by_moodle_course_id(1)
    await bubbles.get_by_id(bubble.id)
    await bubbles.list_by_course_map(map_id)
    assert calls == []


async def test_failed_write_is_rolled_back(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    session: AsyncSession,
    observer: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    map_id = await _make_map(maps)
    bubble = await bubbles.create(map_id, BubbleCreate(activity_id=1, x=0.1, y=0.1))

    async def flush_then_fail() -> None:
        await session.flush()  # the UPDATE reaches the DB inside the transaction...
        raise RuntimeError("boom")  # ...then the commit fails

    monkeypatch.setattr(session, "commit", flush_then_fail)
    with pytest.raises(RuntimeError):
        await bubbles.update_position(bubble.id, 0.9, 0.9)

    # The repository itself rolled back: the failed transaction is gone and
    # the session is immediately reusable (it would still be mid-transaction
    # if the repository had left cleanup to someone else).
    assert not session.in_transaction()
    stored = await observer.get(Bubble, bubble.id)
    assert stored is not None and (stored.x, stored.y) == (0.1, 0.1)
