"""Content bubbles at the database level: the shape check and the partial index."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import BubbleKind
from app.models import Bubble, CourseMap
from app.models.bubble import CK_BUBBLE_KIND_FIELDS, UQ_BUBBLE_ACTIVITY
from app.models.tenant import LEGACY_TENANT_ID
from app.schemas.bubble import BubbleRead

CONTENT: dict[str, Any] = {
    "type": "blocks",
    "title": "Introduccion",
    "autoOpen": "never",
    "body": {"schemaVersion": 1, "blocks": [{"type": "divider"}]},
}


async def _map(session: AsyncSession, course_id: int = 1) -> CourseMap:
    course_map = CourseMap(
        title="Map", moodle_course_id=course_id, image_url="http://img/1.webp"
    )
    session.add(course_map)
    await session.commit()
    return course_map


def _activity(m: CourseMap, activity_id: int = 7) -> Bubble:
    return Bubble(
        course_map_id=m.id,
        moodle_course_id=m.moodle_course_id,
        activity_id=activity_id,
        x=0.1,
        y=0.1,
    )


def _content(m: CourseMap, content: dict[str, Any] | None = None) -> Bubble:
    return Bubble(
        course_map_id=m.id,
        moodle_course_id=m.moodle_course_id,
        kind=BubbleKind.CONTENT,
        content=content or CONTENT,
        x=0.2,
        y=0.2,
    )


async def test_an_existing_style_bubble_is_an_activity_bubble(
    session: AsyncSession,
) -> None:
    bubble = _activity(await _map(session))
    session.add(bubble)
    await session.commit()

    assert bubble.kind is BubbleKind.ACTIVITY
    assert bubble.content is None
    read = BubbleRead.model_validate(bubble)
    assert (read.kind, read.content, read.activity_id) == (
        BubbleKind.ACTIVITY,
        None,
        7,
    )


async def test_a_content_bubble_roundtrips_its_content(session: AsyncSession) -> None:
    bubble = _content(await _map(session))
    session.add(bubble)
    await session.commit()

    stored = await session.get(Bubble, bubble.id, populate_existing=True)
    read = BubbleRead.model_validate(stored)

    assert read.kind is BubbleKind.CONTENT and read.activity_id is None
    assert read.content is not None
    dumped = read.model_dump(mode="json", by_alias=True)["content"]
    assert dumped["title"] == "Introduccion"
    assert dumped["body"] == CONTENT["body"] and dumped["autoOpen"] == "never"


@pytest.mark.parametrize(
    "build",
    [
        # a content bubble that also has an activity,
        lambda m: Bubble(
            course_map_id=m.id,
            moodle_course_id=m.moodle_course_id,
            kind=BubbleKind.CONTENT,
            activity_id=5,
            content=CONTENT,
            x=0,
            y=0,
        ),
        # a content bubble without content,
        lambda m: Bubble(
            course_map_id=m.id,
            moodle_course_id=m.moodle_course_id,
            kind=BubbleKind.CONTENT,
            x=0,
            y=0,
        ),
        # an activity bubble without an activity,
        lambda m: Bubble(
            course_map_id=m.id, moodle_course_id=m.moodle_course_id, x=0, y=0
        ),
        # an activity bubble that carries content.
        lambda m: Bubble(
            course_map_id=m.id,
            moodle_course_id=m.moodle_course_id,
            activity_id=5,
            content=CONTENT,
            x=0,
            y=0,
        ),
    ],
    ids=[
        "content_with_activity",
        "content_without_content",
        "activity_without_activity",
        "activity_with_content",
    ],
)
async def test_the_check_refuses_every_mixed_shape(
    session: AsyncSession, build: Any
) -> None:
    session.add(build(await _map(session)))

    with pytest.raises(IntegrityError, match=CK_BUBBLE_KIND_FIELDS):
        await session.commit()


async def test_the_database_refuses_an_unknown_kind(session: AsyncSession) -> None:
    course_map = await _map(session)

    with pytest.raises(IntegrityError, match=CK_BUBBLE_KIND_FIELDS):
        await session.execute(
            text(
                "INSERT INTO bubbles (id, tenant_id, course_map_id, moodle_course_id,"
                " kind, activity_id, x, y, status, sequence, version, created_at,"
                " updated_at) VALUES (:id, :t, :m, 1, 'video', 3, 0, 0, 'locked', 0,"
                " 1, now(), now())"
            ),
            {"id": uuid.uuid4(), "t": LEGACY_TENANT_ID, "m": course_map.id},
        )


async def test_many_content_bubbles_fit_on_one_map(session: AsyncSession) -> None:
    course_map = await _map(session)
    session.add_all([_content(course_map) for _ in range(3)])

    await session.commit()  # no activity, so nothing to collide on


async def test_content_bubbles_do_not_take_part_in_the_activity_rule(
    session: AsyncSession,
) -> None:
    course_map = await _map(session)
    session.add_all([_activity(course_map, 7), _content(course_map)])
    await session.commit()

    session.add(_activity(course_map, 7))
    with pytest.raises(IntegrityError, match=UQ_BUBBLE_ACTIVITY):
        await session.commit()


async def test_the_activity_rule_still_holds_across_the_maps_of_a_course(
    session: AsyncSession,
) -> None:
    first = await _map(session, 1)
    second = CourseMap(
        title="Two", moodle_course_id=1, image_url="/x", moodle_section_id=9
    )
    session.add(second)
    await session.commit()
    session.add(_activity(first, 7))
    await session.commit()

    session.add(_activity(second, 7))
    with pytest.raises(IntegrityError, match=UQ_BUBBLE_ACTIVITY):
        await session.commit()


async def test_the_activity_index_is_partial_and_unique(session: AsyncSession) -> None:
    definition = (
        await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": UQ_BUBBLE_ACTIVITY},
        )
    ).scalar_one()

    assert "UNIQUE" in definition
    assert "activity_id" in definition
    assert "WHERE" in definition and "'activity'" in definition
