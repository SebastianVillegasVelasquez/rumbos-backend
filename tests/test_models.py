import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import BubbleIcon, BubbleStatus
from app.models import Bubble, CourseMap


async def _map(session: AsyncSession, course_id: int = 1) -> CourseMap:
    course_map = CourseMap(
        title="Map", moodle_course_id=course_id, image_url="http://img/1.webp"
    )
    session.add(course_map)
    await session.commit()
    return course_map


async def test_bubble_defaults_and_enum_roundtrip(session: AsyncSession) -> None:
    course_map = await _map(session)
    bubble = Bubble(
        course_map_id=course_map.id, moodle_course_id=1, activity_id=7, x=0.5, y=0.25
    )
    session.add(bubble)
    await session.commit()
    assert bubble.status is BubbleStatus.LOCKED
    assert bubble.icon is None

    bubble.icon = BubbleIcon.TROPHY
    bubble.status = BubbleStatus.IN_PROGRESS
    await session.commit()
    raw = (
        await session.execute(
            text("SELECT icon, status FROM bubbles WHERE id = :i"), {"i": bubble.id}
        )
    ).one()
    assert tuple(raw) == ("trophy", "in_progress")  # stored as plain strings


async def test_a_course_can_have_several_maps(session: AsyncSession) -> None:
    await _map(session, 5)
    await _map(session, 5)  # no longer unique per course


async def test_section_is_unique_per_course_only_when_set(
    session: AsyncSession,
) -> None:
    def make(course: int, section: int | None) -> CourseMap:
        return CourseMap(
            title="Map",
            moodle_course_id=course,
            moodle_section_id=section,
            image_url="x",
        )

    session.add_all([make(5, None), make(5, None), make(5, 1), make(6, 1)])
    await session.commit()  # NULLs repeat, and so does a section across courses

    session.add(make(5, 1))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_a_bubbles_course_must_match_its_maps(session: AsyncSession) -> None:
    course_map = await _map(session, 5)
    session.add(
        Bubble(
            course_map_id=course_map.id,
            moodle_course_id=6,
            activity_id=1,
            x=0,
            y=0,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_position_is_constrained_to_unit_range(session: AsyncSession) -> None:
    course_map = await _map(session)
    session.add(
        Bubble(
            course_map_id=course_map.id,
            moodle_course_id=1,
            activity_id=1,
            x=1.5,
            y=0.5,
        )
    )
    with pytest.raises(DBAPIError):
        await session.commit()


async def test_deleting_map_cascades_to_bubbles(session: AsyncSession) -> None:
    course_map = await _map(session)
    session.add_all(
        [
            Bubble(
                course_map_id=course_map.id,
                moodle_course_id=1,
                activity_id=i,
                x=0,
                y=0,
            )
            for i in (1, 2)
        ]
    )
    await session.commit()
    await session.delete(course_map)
    await session.commit()
    count = await session.scalar(select(func.count()).select_from(Bubble))
    assert count == 0
