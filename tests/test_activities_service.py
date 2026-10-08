"""`CourseMapService.list_activities` against in-memory fakes, using the real payload.

No HTTP, no database. Variants of the real course-8 fixture are built in code
from a copy of the raw payload.
"""

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
from app.services.course_map_service import (
    CourseMapService,
    is_bubble_candidate,
    is_hidden,
)
from tests.fakes import (
    InMemoryBubbleRepository,
    InMemoryCourseMapRepository,
    InMemoryMoodleClient,
    InMemorySkinRepository,
)
from tests.moodle_fixtures import COURSE8_MODULE_ORDER, course8, parse, raw_course8

COURSE_ID = 8


@pytest.fixture
def moodle() -> InMemoryMoodleClient:
    return InMemoryMoodleClient()


@pytest.fixture
def service(moodle: InMemoryMoodleClient) -> CourseMapService:
    maps = InMemoryCourseMapRepository()
    return CourseMapService(
        maps, InMemoryBubbleRepository(maps), moodle, InMemorySkinRepository()
    )


async def _map_id(service: CourseMapService, course_id: int = COURSE_ID) -> uuid.UUID:
    created = await service.create_course_map(
        CourseMapCreate(title="T", moodle_course_id=course_id, image_url="/u")
    )
    return created.id


def _module(id: int, modname: str = "quiz", **extra: Any) -> MoodleModule:
    fields: dict[str, Any] = {
        "id": id,
        "name": f"{modname} {id}",
        "modname": modname,
        "url": f"https://m.test/mod/{modname}/view.php?id={id}",
        **extra,
    }
    return MoodleModule.model_validate(fields)


async def _setup_course8(
    service: CourseMapService,
    moodle: InMemoryMoodleClient,
    raw: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    moodle.courses[COURSE_ID] = course8() if raw is None else parse(raw)
    return await _map_id(service)


# --- real payload: defaults and includeHidden -------------------------------


async def test_course8_has_no_activities_by_default(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    """Its only section is hidden, so learners see nothing. That is correct."""
    map_id = await _setup_course8(service, moodle)

    assert await service.list_activities(map_id) == []


async def test_course8_include_hidden_returns_all_eight_in_moodle_order(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _setup_course8(service, moodle)

    activities = await service.list_activities(map_id, include_hidden=True)

    assert [a.activity_id for a in activities] == COURSE8_MODULE_ORDER
    assert all(a.hidden for a in activities)
    assert all(
        a.section_name == "Recursos" and a.section_number == 1 for a in activities
    )
    first = activities[0]
    assert first.name == "Preguntas para reconocer cuánto sabes [IN-1]"
    assert first.modname == "quiz"
    assert first.url.endswith("/mod/quiz/view.php?id=28")


# --- id namespaces ----------------------------------------------------------


async def test_module_25_appears_once_and_is_the_quiz_not_the_section(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _setup_course8(service, moodle)

    activities = await service.list_activities(map_id, include_hidden=True)

    matches = [a for a in activities if a.activity_id == 25]
    assert len(matches) == 1
    assert matches[0].name.endswith("[OUT-2]")
    assert matches[0].modname == "quiz"


async def test_bubble_on_activity_25_marks_the_quiz_placed(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _setup_course8(service, moodle)
    bubble = await service.add_bubble(map_id, BubbleCreate(activity_id=25, x=0, y=0))

    activities = await service.list_activities(map_id, include_hidden=True)

    placed = [a for a in activities if a.placed]
    assert [(a.activity_id, a.bubble_id) for a in placed] == [(25, bubble.id)]


async def test_a_section_id_never_marks_anything_placed(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    """A bubble on id 24 (a section id, not a module id) places nothing."""
    map_id = await _setup_course8(service, moodle)
    await service.add_bubble(map_id, BubbleCreate(activity_id=24, x=0, y=0))

    activities = await service.list_activities(map_id, include_hidden=True)

    assert not any(a.placed for a in activities)


async def test_instance_is_not_used_as_activity_id(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    """cmid 28 has instance 7; a bubble on 7 must not place module 28."""
    map_id = await _setup_course8(service, moodle)
    await service.add_bubble(map_id, BubbleCreate(activity_id=7, x=0, y=0))

    activities = await service.list_activities(map_id, include_hidden=True)

    assert not any(a.placed for a in activities)


async def test_bubbles_of_other_courses_do_not_count(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _setup_course8(service, moodle)
    other = await _map_id(service, 99)
    await service.add_bubble(other, BubbleCreate(activity_id=28, x=0, y=0))

    activities = await service.list_activities(map_id, include_hidden=True)

    assert not any(a.placed for a in activities)


async def test_a_bubble_on_another_map_of_the_course_marks_it_placed(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _setup_course8(service, moodle)
    sibling = await _map_id(service)
    bubble = await service.add_bubble(sibling, BubbleCreate(activity_id=28, x=0, y=0))

    activities = await service.list_activities(map_id, include_hidden=True)

    placed = [a for a in activities if a.placed]
    assert [(a.activity_id, a.bubble_id, a.placed_in_map_id) for a in placed] == [
        (28, bubble.id, sibling)
    ]


async def test_activities_carry_the_moodle_section_id(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _setup_course8(service, moodle)

    activities = await service.list_activities(map_id, include_hidden=True)

    assert {a.section_id for a in activities} == {25}  # not 24, not a module id


async def test_only_section_keeps_the_maps_section(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    moodle.courses[COURSE_ID] = [
        MoodleSection(id=10, section=1, modules=[_module(1), _module(2)]),
        MoodleSection(id=2, section=2, modules=[_module(3), _module(10)]),
    ]
    created = await service.create_course_map(
        CourseMapCreate(
            title="T",
            moodle_course_id=COURSE_ID,
            image_url="/u",
            moodle_section_id=10,
        )
    )

    only = await service.list_activities(created.id, only_section=True)
    everything = await service.list_activities(created.id)

    # Section id 10 is section 1's, even though module 10 sits in section 2.
    assert [a.activity_id for a in only] == [1, 2]
    assert [a.activity_id for a in everything] == [1, 2, 3, 10]


async def test_only_section_is_ignored_without_a_section(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    moodle.courses[COURSE_ID] = [
        MoodleSection(id=10, section=1, modules=[_module(1)]),
        MoodleSection(id=11, section=2, modules=[_module(2)]),
    ]
    map_id = await _map_id(service)

    activities = await service.list_activities(map_id, only_section=True)

    assert [a.activity_id for a in activities] == [1, 2]


# --- variants of the real fixture -------------------------------------------


async def test_visible_section_makes_everything_available(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    map_id = await _setup_course8(service, moodle, raw)

    activities = await service.list_activities(map_id)

    assert [a.activity_id for a in activities] == COURSE8_MODULE_ORDER
    assert not any(a.hidden for a in activities)


async def test_hidden_module_in_visible_section(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    raw[1]["modules"][2]["visible"] = 0  # module 29
    map_id = await _setup_course8(service, moodle, raw)

    default = await service.list_activities(map_id)
    with_hidden = await service.list_activities(map_id, include_hidden=True)

    assert 29 not in [a.activity_id for a in default]
    assert len(default) == 7 and not any(a.hidden for a in default)
    assert [a.activity_id for a in with_hidden] == COURSE8_MODULE_ORDER
    assert {a.activity_id for a in with_hidden if a.hidden} == {29}


async def test_user_invisible_module_is_hidden(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    raw[1]["modules"][0]["uservisible"] = False  # module 28
    map_id = await _setup_course8(service, moodle, raw)

    with_hidden = await service.list_activities(map_id, include_hidden=True)

    assert {a.activity_id for a in with_hidden if a.hidden} == {28}


async def test_non_candidates_are_never_listed_even_with_include_hidden(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    modules = raw[1]["modules"]
    modules[0]["modname"] = "label"  # 28
    del modules[1]["url"]  # 27
    modules[2]["noviewlink"] = True  # 29
    map_id = await _setup_course8(service, moodle, raw)

    for include_hidden in (False, True):
        activities = await service.list_activities(
            map_id, include_hidden=include_hidden
        )
        assert [a.activity_id for a in activities] == [31, 30, 32, 25, 33]


async def test_stealth_activity_stays_a_candidate(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    raw[1]["modules"][0]["visibleoncoursepage"] = 0
    map_id = await _setup_course8(service, moodle, raw)

    activities = await service.list_activities(map_id)

    assert 28 in [a.activity_id for a in activities]
    assert not any(a.hidden for a in activities)


async def test_non_null_availability_passes_through_uninterpreted(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    raw = raw_course8()
    raw[1]["visible"] = 1
    raw[1]["modules"][0]["availability"] = '{"op":"&","c":[{"type":"date"}]}'
    map_id = await _setup_course8(service, moodle, raw)

    activities = await service.list_activities(map_id)

    assert [a.activity_id for a in activities] == COURSE8_MODULE_ORDER
    assert not any(a.hidden for a in activities)


async def test_order_is_preserved_across_sections(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _map_id(service)
    moodle.courses[COURSE_ID] = [
        MoodleSection(id=1, name="A", section=0, modules=[_module(50), _module(10)]),
        MoodleSection(id=2, name="B", section=1, modules=[_module(40, "url")]),
        MoodleSection(id=3, name="Empty", section=2),
    ]

    activities = await service.list_activities(map_id)

    assert [(a.activity_id, a.section_number) for a in activities] == [
        (50, 0),
        (10, 0),
        (40, 1),
    ]


# --- rule functions ---------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "modname", "expected"),
    [
        ({}, "quiz", True),
        ({}, "label", False),
        ({"url": None}, "quiz", False),
        ({"url": ""}, "quiz", False),
        ({"noviewlink": True}, "quiz", False),
        ({"noviewlink": False}, "scorm", True),
        ({"visibleoncoursepage": 0}, "quiz", True),
        ({"candisplay": False}, "quiz", True),
    ],
)
def test_is_bubble_candidate(
    extra: dict[str, Any], modname: str, expected: bool
) -> None:
    assert is_bubble_candidate(_module(1, modname, **extra)) is expected


@pytest.mark.parametrize(
    ("section_visible", "module_extra", "expected"),
    [
        (True, {}, False),
        (False, {}, True),  # hidden section, module flags all "visible"
        (True, {"visible": 0}, True),
        (True, {"uservisible": False}, True),
        (True, {"visible": 1, "uservisible": True}, False),
    ],
)
def test_is_hidden(
    section_visible: bool, module_extra: dict[str, Any], expected: bool
) -> None:
    section = MoodleSection(id=1, visible=section_visible)
    assert is_hidden(section, _module(1, **module_extra)) is expected


# --- errors -----------------------------------------------------------------


async def test_unknown_map_does_not_call_moodle(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    moodle.error = MoodleUnavailableError("would fail if called")
    with pytest.raises(CourseMapNotFoundError):
        await service.list_activities(uuid.uuid4())


async def test_moodle_errors_propagate_unchanged(
    service: CourseMapService, moodle: InMemoryMoodleClient
) -> None:
    map_id = await _map_id(service)
    with pytest.raises(MoodleCourseNotFoundError):  # course absent in the fake
        await service.list_activities(map_id)
    moodle.error = MoodleAuthError("nope")
    with pytest.raises(MoodleAuthError):
        await service.list_activities(map_id)
