"""`MapResolutionService` against in-memory fakes and the real course-8 payload.

Moodle is the real `CachedMoodleClient` over a fake upstream and a fake clock,
so `moodleStatus` is exercised end to end (live / cached / unavailable).
"""

import uuid
from typing import Any

import pytest

from app.enums import Availability, MoodleStatus
from app.exceptions import CourseMapNotFoundError
from app.moodle.cache import CachedMoodleClient
from app.moodle.exceptions import (
    MoodleAuthError,
    MoodleCourseNotFoundError,
    MoodleError,
    MoodleUnavailableError,
)
from app.schemas.bubble import BubbleCreate
from app.schemas.course_map import CourseMapCreate
from app.schemas.resolved import ResolvedMap
from app.services.resolution_service import MapResolutionService
from tests.fakes import (
    FakeClock,
    InMemoryBubbleRepository,
    InMemoryCourseMapRepository,
    InMemoryMoodleClient,
)
from tests.moodle_fixtures import course8, parse, raw_course8

COURSE_ID = 8
TTL = 60.0
STALE_MAX = 3600.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def upstream() -> InMemoryMoodleClient:
    fake = InMemoryMoodleClient()
    fake.courses[COURSE_ID] = course8()
    return fake


@pytest.fixture
def maps() -> InMemoryCourseMapRepository:
    return InMemoryCourseMapRepository()


@pytest.fixture
def bubbles(maps: InMemoryCourseMapRepository) -> InMemoryBubbleRepository:
    return InMemoryBubbleRepository(maps)


@pytest.fixture
def service(
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
    clock: FakeClock,
) -> MapResolutionService:
    cache = CachedMoodleClient(
        upstream, ttl_seconds=TTL, stale_max_seconds=STALE_MAX, clock=clock
    )
    return MapResolutionService(maps, bubbles, cache)


async def _map_with_bubbles(
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    activity_ids: list[int],
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    created = await maps.create(
        CourseMapCreate(title="T", moodle_course_id=COURSE_ID, image_url="/u")
    )
    ids = [
        (
            await bubbles.create(
                created.id, COURSE_ID, BubbleCreate(activity_id=a, x=0.5, y=0.5)
            )
        ).id
        for a in activity_ids
    ]
    return created.id, ids


def _visible_course() -> list[Any]:
    """Course 8 with its hidden section made visible."""
    raw = raw_course8()
    raw[1]["visible"] = 1
    return raw


def _by_bubble(resolved: ResolvedMap) -> dict[uuid.UUID, Availability]:
    return {b.bubble_id: b.availability for b in resolved.bubbles}


# --- availability ----------------------------------------------------------


async def test_available_bubble_carries_the_full_activity(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    upstream.courses[COURSE_ID] = parse(_visible_course())
    map_id, [bubble_id] = await _map_with_bubbles(maps, bubbles, [28])

    resolved = await service.resolve(map_id)

    assert resolved.moodle_status is MoodleStatus.LIVE
    [entry] = resolved.bubbles
    assert entry.bubble_id == bubble_id
    assert entry.availability is Availability.AVAILABLE
    assert entry.activity is not None
    assert entry.activity.model_dump() == {
        "activity_id": 28,
        "name": "Preguntas para reconocer cuánto sabes [IN-1]",
        "modname": "quiz",
        "section_name": "Recursos",
        "section_number": 1,
        "url": "https://academiaturismo.mincit.gov.co/mod/quiz/view.php?id=28",
    }


async def test_hidden_bubble_hides_its_activity_unless_asked(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
) -> None:
    # As shipped, course 8's only content section is hidden.
    map_id, [bubble_id] = await _map_with_bubbles(maps, bubbles, [28])

    default = await service.resolve(map_id)
    explicit_false = await service.resolve(map_id, include_hidden=False)
    with_hidden = await service.resolve(map_id, include_hidden=True)

    for resolved in (default, explicit_false):
        assert resolved.bubbles[0].availability is Availability.HIDDEN
        assert resolved.bubbles[0].activity is None  # name must not leak
    assert with_hidden.bubbles[0].availability is Availability.HIDDEN
    assert with_hidden.bubbles[0].activity is not None
    assert with_hidden.bubbles[0].activity.name.endswith("[IN-1]")
    assert with_hidden.bubbles[0].bubble_id == bubble_id


async def test_module_level_hiding_is_hidden_in_a_visible_section(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    raw = _visible_course()
    raw[1]["modules"][0]["visible"] = 0  # module 28
    raw[1]["modules"][1]["uservisible"] = False  # module 27
    upstream.courses[COURSE_ID] = parse(raw)
    map_id, ids = await _map_with_bubbles(maps, bubbles, [28, 27, 29])

    resolved = await service.resolve(map_id)

    assert [b.availability for b in resolved.bubbles] == [
        Availability.HIDDEN,
        Availability.HIDDEN,
        Availability.AVAILABLE,
    ]
    assert [b.bubble_id for b in resolved.bubbles] == ids


async def test_unknown_activity_is_missing(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    upstream.courses[COURSE_ID] = parse(_visible_course())
    map_id, _ = await _map_with_bubbles(maps, bubbles, [9999])

    resolved = await service.resolve(map_id, include_hidden=True)

    assert resolved.moodle_status is MoodleStatus.LIVE
    assert resolved.bubbles[0].availability is Availability.MISSING
    assert resolved.bubbles[0].activity is None


async def test_non_candidates_are_missing_even_when_found(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    raw = _visible_course()
    base = {"visible": 1, "uservisible": True, "completion": 0}
    raw[1]["modules"] += [
        {"id": 500, "name": "Text", "modname": "label", "url": "https://m/l", **base},
        {"id": 501, "name": "No url", "modname": "page", **base},
        {
            "id": 502,
            "name": "Inline",
            "modname": "page",
            "url": "https://m/p",
            "noviewlink": True,
            **base,
        },
    ]
    upstream.courses[COURSE_ID] = parse(raw)
    map_id, _ = await _map_with_bubbles(maps, bubbles, [500, 501, 502])

    resolved = await service.resolve(map_id, include_hidden=True)

    assert {b.availability for b in resolved.bubbles} == {Availability.MISSING}
    assert all(b.activity is None for b in resolved.bubbles)


async def test_a_hidden_non_candidate_is_missing_not_hidden(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    raw = raw_course8()  # section stays hidden
    raw[1]["modules"].append(
        {"id": 500, "name": "Text", "modname": "label", "url": "https://m/l"}
    )
    upstream.courses[COURSE_ID] = parse(raw)
    map_id, _ = await _map_with_bubbles(maps, bubbles, [500])

    resolved = await service.resolve(map_id, include_hidden=True)

    assert resolved.bubbles[0].availability is Availability.MISSING


# --- shape -----------------------------------------------------------------


async def test_one_entry_per_bubble_in_order_and_nothing_else(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    upstream.courses[COURSE_ID] = parse(_visible_course())  # 8 candidates
    map_id, ids = await _map_with_bubbles(maps, bubbles, [33, 9999, 28])

    resolved = await service.resolve(map_id)

    assert [b.bubble_id for b in resolved.bubbles] == ids
    assert [b.availability for b in resolved.bubbles] == [
        Availability.AVAILABLE,
        Availability.MISSING,
        Availability.AVAILABLE,
    ]


async def test_map_without_bubbles_resolves_to_an_empty_list(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
) -> None:
    map_id, _ = await _map_with_bubbles(maps, bubbles, [])
    resolved = await service.resolve(map_id)
    assert resolved.bubbles == [] and resolved.moodle_status is MoodleStatus.LIVE


async def test_unknown_map_raises(service: MapResolutionService) -> None:
    with pytest.raises(CourseMapNotFoundError):
        await service.resolve(uuid.uuid4())


# --- cmid / section id collision -------------------------------------------


async def test_module_ids_are_never_confused_with_section_ids(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    """Course 8 has a section with id 25 *and* a quiz module with id 25.

    Section ids are 24 (General) and 25 (Recursos): neither may be treated as
    an activity, and module 25 must resolve to the quiz, in section 'Recursos'.
    """
    upstream.courses[COURSE_ID] = parse(_visible_course())
    map_id, _ = await _map_with_bubbles(maps, bubbles, [25, 24])

    quiz, general_section_id = (await service.resolve(map_id)).bubbles

    assert quiz.availability is Availability.AVAILABLE
    assert quiz.activity is not None
    assert quiz.activity.activity_id == 25
    assert quiz.activity.name.endswith("[OUT-2]")
    assert quiz.activity.modname == "quiz"
    assert (quiz.activity.section_name, quiz.activity.section_number) == (
        "Recursos",
        1,
    )
    # 24 is only a section id here: there is no module 24.
    assert general_section_id.availability is Availability.MISSING
    assert general_section_id.activity is None


async def test_module_id_equal_to_a_section_id_in_the_real_payload_is_hidden(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
) -> None:
    """Untouched course 8: module 25 lives in the hidden section 25."""
    map_id, _ = await _map_with_bubbles(maps, bubbles, [25])
    resolved = await service.resolve(map_id, include_hidden=True)
    assert resolved.bubbles[0].availability is Availability.HIDDEN
    assert resolved.bubbles[0].activity is not None
    assert resolved.bubbles[0].activity.name.endswith("[OUT-2]")


# --- Moodle status and failures --------------------------------------------


async def test_cache_hit_inside_ttl_is_live(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
    clock: FakeClock,
) -> None:
    map_id, _ = await _map_with_bubbles(maps, bubbles, [28])
    await service.resolve(map_id)
    clock.advance(TTL - 1)
    upstream.error = MoodleUnavailableError("down")  # would matter if it were called

    resolved = await service.resolve(map_id)

    assert resolved.moodle_status is MoodleStatus.LIVE
    assert upstream.calls == 1


async def test_moodle_down_with_a_cached_copy_serves_it_as_cached(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
    clock: FakeClock,
) -> None:
    upstream.courses[COURSE_ID] = parse(_visible_course())
    map_id, _ = await _map_with_bubbles(maps, bubbles, [28, 9999])
    await service.resolve(map_id)
    clock.advance(TTL + 1)
    upstream.error = MoodleUnavailableError("down")

    resolved = await service.resolve(map_id)

    assert resolved.moodle_status is MoodleStatus.CACHED
    assert [b.availability for b in resolved.bubbles] == [
        Availability.AVAILABLE,
        Availability.MISSING,
    ]
    assert resolved.bubbles[0].activity is not None


async def test_moodle_down_without_a_cache_makes_everything_unknown(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    map_id, ids = await _map_with_bubbles(maps, bubbles, [28, 9999])
    upstream.error = MoodleUnavailableError("down")

    resolved = await service.resolve(map_id, include_hidden=True)

    assert resolved.moodle_status is MoodleStatus.UNAVAILABLE
    assert [b.bubble_id for b in resolved.bubbles] == ids
    assert {b.availability for b in resolved.bubbles} == {Availability.UNKNOWN}
    assert all(b.activity is None for b in resolved.bubbles)


async def test_cache_older_than_the_stale_limit_is_unavailable(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
    clock: FakeClock,
) -> None:
    map_id, _ = await _map_with_bubbles(maps, bubbles, [28])
    await service.resolve(map_id)
    clock.advance(STALE_MAX + 1)
    upstream.error = MoodleUnavailableError("down")

    resolved = await service.resolve(map_id)

    assert resolved.moodle_status is MoodleStatus.UNAVAILABLE
    assert resolved.bubbles[0].availability is Availability.UNKNOWN


@pytest.mark.parametrize("error", [MoodleAuthError("x"), MoodleError("x")])
async def test_auth_and_config_errors_degrade_instead_of_raising(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
    error: MoodleError,
) -> None:
    map_id, _ = await _map_with_bubbles(maps, bubbles, [28])
    upstream.error = error

    resolved = await service.resolve(map_id)

    assert resolved.moodle_status is MoodleStatus.UNAVAILABLE
    assert resolved.bubbles[0].availability is Availability.UNKNOWN


async def test_course_gone_from_moodle_makes_every_bubble_missing(
    service: MapResolutionService,
    maps: InMemoryCourseMapRepository,
    bubbles: InMemoryBubbleRepository,
    upstream: InMemoryMoodleClient,
) -> None:
    map_id, _ = await _map_with_bubbles(maps, bubbles, [28, 25])
    upstream.error = MoodleCourseNotFoundError("gone")

    resolved = await service.resolve(map_id, include_hidden=True)

    assert resolved.moodle_status is MoodleStatus.LIVE  # Moodle did answer
    assert {b.availability for b in resolved.bubbles} == {Availability.MISSING}
