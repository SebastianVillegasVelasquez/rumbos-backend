"""Integration tests: repositories against real Postgres.

A second, independent session is used to observe what was really committed.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.enums import BubbleIcon, BubbleStatus
from app.exceptions import (
    ActivityAlreadyPlacedError,
    CourseMapNotFoundError,
    SectionAlreadyMappedError,
)
from app.models import Bubble, CourseMap
from app.repositories.sqlalchemy.bubble_repository import SqlAlchemyBubbleRepository
from app.repositories.sqlalchemy.course_map_repository import (
    SqlAlchemyCourseMapRepository,
)
from app.schemas.bubble import BubbleCreate, BubbleUpdate
from app.schemas.course_map import CourseMapCreate, CourseMapUpdate
from app.services.course_map_service import CourseMapService
from tests.fakes import InMemoryMoodleClient


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
        await maps.create(
            CourseMapCreate(title="T", moodle_course_id=course_id, image_url="/u")
        )
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
    created = await maps.create(
        CourseMapCreate(title="T", moodle_course_id=9, image_url="/img")
    )
    assert created.id.version == 7
    stored = await observer.get(CourseMap, created.id)
    assert stored is not None and stored.moodle_course_id == 9


async def test_duplicate_section_raises_and_rolls_back(
    maps: SqlAlchemyCourseMapRepository,
    session: AsyncSession,
    observer: AsyncSession,
) -> None:
    await maps.create(
        CourseMapCreate(
            title="T", moodle_course_id=3, image_url="/a", moodle_section_id=11
        )
    )
    with pytest.raises(SectionAlreadyMappedError):
        await maps.create(
            CourseMapCreate(
                title="T", moodle_course_id=3, image_url="/b", moodle_section_id=11
            )
        )
    # The session is usable again (rolled back) and nothing extra persisted.
    assert await _count(session, CourseMap) == 1
    assert await _count(observer, CourseMap) == 1


async def test_a_course_can_have_several_maps_and_sections_are_per_course(
    maps: SqlAlchemyCourseMapRepository,
) -> None:
    def make(course: int, section: int | None) -> CourseMapCreate:
        return CourseMapCreate(
            title="T",
            moodle_course_id=course,
            image_url="/a",
            moodle_section_id=section,
        )

    first = await maps.create(make(3, 11))
    second = await maps.create(make(3, 12))
    third = await maps.create(make(3, None))
    fourth = await maps.create(make(3, None))  # NULL sections never collide
    other_course = await maps.create(make(4, 11))  # same section id, other course

    assert [m.position for m in (first, second, third, fourth)] == [0, 1, 2, 3]
    assert other_course.position == 0


async def test_get_by_id(maps: SqlAlchemyCourseMapRepository) -> None:
    map_id = await _make_map(maps, 4)
    by_id = await maps.get_by_id(map_id)
    assert by_id is not None and by_id.moodle_course_id == 4
    assert await maps.get_by_id(uuid.uuid4()) is None


async def test_delete_map_cascades_and_commits(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    await bubbles.create(map_id, 1, BubbleCreate(activity_id=1, x=0.1, y=0.1))
    assert await maps.delete(map_id) is True
    assert await _count(observer, CourseMap) == 0
    assert await _count(observer, Bubble) == 0
    assert await maps.delete(map_id) is False


async def _seed_maps(
    session: AsyncSession, titles: list[tuple[int, str]]
) -> dict[int, uuid.UUID]:
    """Maps with strictly increasing `updated_at` (first = oldest)."""
    ids: dict[int, uuid.UUID] = {}
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for n, (course_id, title) in enumerate(titles):
        course_map = CourseMap(
            title=title,
            moodle_course_id=course_id,
            image_url="/x",
            updated_at=base + timedelta(minutes=n),
        )
        session.add(course_map)
        await session.flush()
        ids[course_id] = course_map.id
    await session.commit()
    return ids


async def test_list_orders_by_updated_at_desc_and_paginates(
    maps: SqlAlchemyCourseMapRepository, session: AsyncSession
) -> None:
    await _seed_maps(session, [(c, f"Map {c}") for c in range(1, 6)])

    page1, total = await maps.list(None, None, limit=2, offset=0)
    page2, _ = await maps.list(None, None, limit=2, offset=2)
    page3, _ = await maps.list(None, None, limit=2, offset=4)
    beyond, beyond_total = await maps.list(None, None, limit=2, offset=10)

    assert total == 5
    assert [m.moodle_course_id for m in page1] == [5, 4]
    assert [m.moodle_course_id for m in page2] == [3, 2]
    assert [m.moodle_course_id for m in page3] == [1]
    assert beyond == [] and beyond_total == 5


async def test_list_filters_by_course_and_title_with_total(
    maps: SqlAlchemyCourseMapRepository, session: AsyncSession
) -> None:
    await _seed_maps(
        session,
        [(1, "Ruta del Café"), (2, "Ruta del mar"), (3, "Otra cosa"), (4, "100%_real")],
    )

    by_course, total = await maps.list(2, None, 24, 0)
    assert [m.moodle_course_id for m in by_course] == [2] and total == 1

    by_title, total = await maps.list(None, "RUTA", 24, 0)  # case-insensitive
    assert [m.moodle_course_id for m in by_title] == [2, 1] and total == 2

    both, total = await maps.list(1, "ruta", 24, 0)
    assert [m.moodle_course_id for m in both] == [1] and total == 1

    none, total = await maps.list(3, "ruta", 24, 0)
    assert none == [] and total == 0

    # LIKE wildcards in the search text are literal.
    wild, total = await maps.list(None, "%", 24, 0)
    assert [m.moodle_course_id for m in wild] == [4] and total == 1
    wild, total = await maps.list(None, "_", 24, 0)
    assert [m.moodle_course_id for m in wild] == [4] and total == 1


async def test_list_total_ignores_limit_and_offset(
    maps: SqlAlchemyCourseMapRepository, session: AsyncSession
) -> None:
    await _seed_maps(session, [(c, f"Mapa {c}") for c in range(1, 8)])
    page, total = await maps.list(None, "mapa", limit=3, offset=3)
    assert len(page) == 3 and total == 7


async def test_list_counts_bubbles_in_one_query(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    session: AsyncSession,
    engine: AsyncEngine,
) -> None:
    ids = await _seed_maps(session, [(1, "A"), (2, "B"), (3, "C")])
    for activity in (1, 2, 3):
        await bubbles.create(ids[1], 1, BubbleCreate(activity_id=activity, x=0, y=0))
    await bubbles.create(ids[2], 2, BubbleCreate(activity_id=1, x=0, y=0))

    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        items, _ = await maps.list(None, None, 24, 0)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert {m.moodle_course_id: m.bubble_count for m in items} == {1: 3, 2: 1, 3: 0}
    # The page and the total: two statements however many maps there are.
    assert len(statements) == 2


async def test_map_update_applies_only_sent_fields_in_one_commit(
    maps: SqlAlchemyCourseMapRepository,
    session: AsyncSession,
    observer: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    map_id = await _make_map(maps)
    before = await maps.get_by_id(map_id)
    assert before is not None
    calls = _spy_commits(session, monkeypatch)

    renamed = await maps.update(map_id, CourseMapUpdate(title="Nuevo"))

    assert calls == [1]
    assert renamed is not None and renamed.title == "Nuevo"
    assert renamed.image_url == before.image_url  # not sent, not changed
    assert renamed.updated_at > before.updated_at
    assert renamed.created_at == before.created_at
    both = await maps.update(
        map_id, CourseMapUpdate(title="Otro", image_url="https://i/new.png")
    )
    assert both is not None and both.image_url == "https://i/new.png"
    stored = await observer.get(CourseMap, map_id)
    assert stored is not None
    assert (stored.title, stored.image_url) == ("Otro", "https://i/new.png")


async def test_map_update_on_missing_map_returns_none(
    maps: SqlAlchemyCourseMapRepository,
) -> None:
    assert await maps.update(uuid.uuid4(), CourseMapUpdate(title="x")) is None


async def test_map_update_is_all_or_nothing(
    maps: SqlAlchemyCourseMapRepository,
    session: AsyncSession,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    # Make exactly one of the patched fields unacceptable to the database.
    await session.execute(
        text(
            "ALTER TABLE course_maps ADD CONSTRAINT ck_no_forbidden "
            "CHECK (title <> 'forbidden')"
        )
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await maps.update(
            map_id, CourseMapUpdate(image_url="/changed", title="forbidden")
        )

    assert not session.in_transaction()  # rolled back, session reusable
    stored = await observer.get(CourseMap, map_id)
    assert stored is not None
    assert (stored.title, stored.image_url) == ("T", "/u")


async def test_map_delete_cascades_to_every_bubble_but_only_its_own(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    doomed, kept = await _make_map(maps, 1), await _make_map(maps, 2)
    for activity in (1, 2, 3):
        await bubbles.create(doomed, 1, BubbleCreate(activity_id=activity, x=0, y=0))
    await bubbles.create(kept, 2, BubbleCreate(activity_id=1, x=0, y=0))

    assert await maps.delete(doomed) is True

    remaining = (await observer.scalars(select(Bubble.course_map_id))).all()
    assert list(remaining) == [kept]


# --- bubbles ---------------------------------------------------------------


async def test_bubble_create_and_list_in_creation_order(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    for activity in (10, 20, 30):
        await bubbles.create(
            map_id, 1, BubbleCreate(activity_id=activity, x=0.5, y=0.5)
        )
    listed = await bubbles.list_by_course_map(map_id)
    assert [b.activity_id for b in listed] == [10, 20, 30]
    assert await _count(observer, Bubble) == 3
    assert await bubbles.list_by_course_map(uuid.uuid4()) == []


async def test_bubble_create_for_missing_map_raises_and_rolls_back(
    bubbles: SqlAlchemyBubbleRepository, session: AsyncSession
) -> None:
    with pytest.raises(CourseMapNotFoundError):
        await bubbles.create(uuid.uuid4(), 1, BubbleCreate(activity_id=1, x=0, y=0))
    assert await _count(session, Bubble) == 0


async def test_bubble_update_commits_and_touches_updated_at(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    bubble = await bubbles.create(map_id, 1, BubbleCreate(activity_id=1, x=0.1, y=0.2))

    moved = await bubbles.update(
        bubble.id, BubbleUpdate.model_validate({"x": 0.7, "y": 0.8})
    )
    assert moved is not None and (moved.x, moved.y) == (0.7, 0.8)
    assert moved.updated_at > bubble.updated_at
    iconed = await bubbles.update(bubble.id, BubbleUpdate(icon=BubbleIcon.STAR))
    assert iconed is not None and iconed.icon is BubbleIcon.STAR
    assert (iconed.x, iconed.y) == (0.7, 0.8)  # earlier move not clobbered
    # Several fields at once, including clearing the icon, in one call.
    both = await bubbles.update(
        bubble.id, BubbleUpdate(icon=None, status=BubbleStatus.COMPLETE)
    )
    assert both is not None and both.icon is None
    assert both.status is BubbleStatus.COMPLETE

    stored = await observer.get(Bubble, bubble.id)
    assert stored is not None
    assert (stored.x, stored.y, stored.icon, stored.status) == (
        0.7,
        0.8,
        None,
        BubbleStatus.COMPLETE,
    )


async def test_bubble_update_and_delete_on_missing_bubble(
    bubbles: SqlAlchemyBubbleRepository,
) -> None:
    missing = uuid.uuid4()
    patch = BubbleUpdate(status=BubbleStatus.LOCKED)
    assert await bubbles.update(missing, patch) is None
    assert await bubbles.delete(missing) is False


async def test_bubble_delete_commits(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    bubble = await bubbles.create(map_id, 1, BubbleCreate(activity_id=1, x=0, y=0))
    assert await bubbles.delete(bubble.id) is True
    assert await _count(observer, Bubble) == 0
    assert await bubbles.get_by_id(bubble.id) is None


async def test_duplicate_activity_on_a_map_raises_and_rolls_back(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    session: AsyncSession,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    await bubbles.create(map_id, 1, BubbleCreate(activity_id=7, x=0.1, y=0.1))

    with pytest.raises(ActivityAlreadyPlacedError):
        await bubbles.create(map_id, 1, BubbleCreate(activity_id=7, x=0.9, y=0.9))

    assert not session.in_transaction()
    assert await _count(observer, Bubble) == 1
    # The session still works afterwards.
    await bubbles.create(map_id, 1, BubbleCreate(activity_id=8, x=0.2, y=0.2))
    assert await _count(observer, Bubble) == 2


async def test_same_activity_on_maps_of_different_courses_is_allowed(
    maps: SqlAlchemyCourseMapRepository, bubbles: SqlAlchemyBubbleRepository
) -> None:
    first, second = await _make_map(maps, 1), await _make_map(maps, 2)
    await bubbles.create(first, 1, BubbleCreate(activity_id=7, x=0, y=0))
    await bubbles.create(second, 2, BubbleCreate(activity_id=7, x=0, y=0))


async def test_activity_is_unique_per_course_across_its_maps(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    observer: AsyncSession,
) -> None:
    first, second = await _make_map(maps, 1), await _make_map(maps, 1)
    await bubbles.create(first, 1, BubbleCreate(activity_id=7, x=0, y=0))

    with pytest.raises(ActivityAlreadyPlacedError) as caught:
        await bubbles.create(second, 1, BubbleCreate(activity_id=7, x=0, y=0))

    assert caught.value.course_map_id == first
    assert caught.value.course_map_title == "T"
    assert await _count(observer, Bubble) == 1


async def test_deleting_a_map_frees_its_activities_for_the_course(
    maps: SqlAlchemyCourseMapRepository, bubbles: SqlAlchemyBubbleRepository
) -> None:
    first, second = await _make_map(maps, 1), await _make_map(maps, 1)
    await bubbles.create(first, 1, BubbleCreate(activity_id=7, x=0, y=0))
    await maps.delete(first)

    await bubbles.create(second, 1, BubbleCreate(activity_id=7, x=0, y=0))


async def test_bubble_course_must_match_its_map(
    maps: SqlAlchemyCourseMapRepository, bubbles: SqlAlchemyBubbleRepository
) -> None:
    map_id = await _make_map(maps, 1)
    with pytest.raises(CourseMapNotFoundError):
        await bubbles.create(map_id, 2, BubbleCreate(activity_id=7, x=0, y=0))


async def test_concurrent_duplicate_creates_yield_exactly_one_bubble(
    maps: SqlAlchemyCourseMapRepository,
    engine: AsyncEngine,
    observer: AsyncSession,
) -> None:
    """Relies on the DB constraint: a pre-check alone would let several win."""
    map_id = await _make_map(maps)

    async def attempt(x: float) -> str:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            try:
                await SqlAlchemyBubbleRepository(s).create(
                    map_id, 1, BubbleCreate(activity_id=42, x=x, y=0.5)
                )
            except ActivityAlreadyPlacedError:
                return "duplicate"
            return "created"

    outcomes = await asyncio.gather(*(attempt(i / 10) for i in range(8)))

    assert sorted(outcomes) == ["created"] + ["duplicate"] * 7
    assert await _count(observer, Bubble) == 1


# --- commit / rollback placement -------------------------------------------


async def test_read_methods_never_commit(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    map_id = await _make_map(maps)
    bubble = await bubbles.create(map_id, 1, BubbleCreate(activity_id=1, x=0, y=0))
    calls = _spy_commits(session, monkeypatch)

    await maps.get_by_id(map_id)
    await maps.list_for_course(1)
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
    bubble = await bubbles.create(map_id, 1, BubbleCreate(activity_id=1, x=0.1, y=0.1))

    async def flush_then_fail() -> None:
        await session.flush()  # the UPDATE reaches the DB inside the transaction...
        raise RuntimeError("boom")  # ...then the commit fails

    monkeypatch.setattr(session, "commit", flush_then_fail)
    with pytest.raises(RuntimeError):
        await bubbles.update(bubble.id, BubbleUpdate(status=BubbleStatus.COMPLETE))

    # The repository itself rolled back: the failed transaction is gone and
    # the session is immediately reusable (it would still be mid-transaction
    # if the repository had left cleanup to someone else).
    assert not session.in_transaction()
    stored = await observer.get(Bubble, bubble.id)
    assert stored is not None and stored.status is BubbleStatus.LOCKED


async def test_multi_field_update_is_all_or_nothing(
    maps: SqlAlchemyCourseMapRepository,
    bubbles: SqlAlchemyBubbleRepository,
    session: AsyncSession,
    observer: AsyncSession,
) -> None:
    map_id = await _make_map(maps)
    bubble = await bubbles.create(
        map_id, 1, BubbleCreate(activity_id=1, x=0.1, y=0.2, icon=BubbleIcon.STAR)
    )
    # Make exactly one of the patched fields unacceptable to the database.
    await session.execute(
        text(
            "ALTER TABLE bubbles ADD CONSTRAINT ck_no_complete "
            "CHECK (status <> 'complete')"
        )
    )
    await session.commit()

    patch = BubbleUpdate.model_validate(
        {"x": 0.9, "y": 0.8, "icon": None, "status": "complete"}
    )
    service = CourseMapService(maps, bubbles, InMemoryMoodleClient())
    with pytest.raises(IntegrityError):
        await service.update_bubble(map_id, bubble.id, patch)

    # Not just the failing field: none of them may have changed.
    stored = await observer.get(Bubble, bubble.id)
    assert stored is not None
    assert (stored.x, stored.y, stored.icon, stored.status) == (
        0.1,
        0.2,
        BubbleIcon.STAR,
        BubbleStatus.LOCKED,
    )
