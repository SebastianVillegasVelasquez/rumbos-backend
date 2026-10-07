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
    bubble = Bubble(course_map_id=course_map.id, activity_id=7, x=0.5, y=0.25)
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


async def test_moodle_course_id_is_unique(session: AsyncSession) -> None:
    await _map(session, 5)
    session.add(CourseMap(title="Map", moodle_course_id=5, image_url="x"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_position_is_constrained_to_unit_range(session: AsyncSession) -> None:
    course_map = await _map(session)
    session.add(Bubble(course_map_id=course_map.id, activity_id=1, x=1.5, y=0.5))
    with pytest.raises(DBAPIError):
        await session.commit()


async def test_deleting_map_cascades_to_bubbles(session: AsyncSession) -> None:
    course_map = await _map(session)
    session.add_all(
        [Bubble(course_map_id=course_map.id, activity_id=i, x=0, y=0) for i in (1, 2)]
    )
    await session.commit()
    await session.delete(course_map)
    await session.commit()
    count = await session.scalar(select(func.count()).select_from(Bubble))
    assert count == 0
