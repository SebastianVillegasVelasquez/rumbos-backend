"""`CachedMoodleClient` with a fake clock and a fake upstream: no sleeping, no HTTP."""

import asyncio
import logging

import pytest

from app.moodle.cache import CachedMoodleClient
from app.moodle.exceptions import (
    MoodleAuthError,
    MoodleCourseNotFoundError,
    MoodleError,
    MoodleUnavailableError,
)
from tests.fakes import FakeClock, InMemoryMoodleClient
from tests.moodle_fixtures import course8

TTL = 60.0
STALE_MAX = 3600.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def upstream() -> InMemoryMoodleClient:
    fake = InMemoryMoodleClient()
    fake.courses[8] = course8()
    return fake


@pytest.fixture
def cache(upstream: InMemoryMoodleClient, clock: FakeClock) -> CachedMoodleClient:
    return CachedMoodleClient(
        upstream, ttl_seconds=TTL, stale_max_seconds=STALE_MAX, clock=clock
    )


async def _let_tasks_run() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_hit_inside_ttl_does_not_call_upstream(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    first = await cache.fetch_course_contents(8)
    clock.advance(TTL - 1)
    second = await cache.fetch_course_contents(8)

    assert upstream.calls == 1
    assert first.stale is False and second.stale is False
    assert [s.id for s in second.sections] == [s.id for s in course8()]


async def test_expired_entry_is_refetched(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    await cache.fetch_course_contents(8)
    clock.advance(TTL)  # exactly at the TTL counts as expired

    result = await cache.fetch_course_contents(8)

    assert upstream.calls == 2
    assert result.stale is False


async def test_courses_are_cached_independently(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient
) -> None:
    upstream.courses[9] = []
    await cache.fetch_course_contents(8)
    await cache.fetch_course_contents(9)
    await cache.fetch_course_contents(8)
    assert upstream.calls == 2


async def test_plain_client_interface_uses_the_same_cache(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient
) -> None:
    sections = await cache.get_course_contents(8)
    await cache.fetch_course_contents(8)
    assert [s.id for s in sections] == [s.id for s in course8()]
    assert upstream.calls == 1
    assert (await cache.get_site_info()).sitename == "Fake Moodle"


async def test_callers_cannot_corrupt_the_cached_list(
    cache: CachedMoodleClient,
) -> None:
    first = await cache.fetch_course_contents(8)
    first.sections.clear()
    assert len((await cache.fetch_course_contents(8)).sections) == len(course8())


async def test_single_flight_shares_one_upstream_call(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient
) -> None:
    upstream.gate = asyncio.Event()
    tasks = [asyncio.create_task(cache.fetch_course_contents(8)) for _ in range(20)]
    await _let_tasks_run()
    assert upstream.calls == 1  # all 20 are waiting on the one call

    upstream.gate.set()
    results = await asyncio.gather(*tasks)

    assert upstream.calls == 1
    assert all(not r.stale for r in results)
    assert all(len(r.sections) == len(course8()) for r in results)
    # The burst is over: the next call is a plain cache hit.
    await cache.fetch_course_contents(8)
    assert upstream.calls == 1


async def test_single_flight_is_per_course(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient
) -> None:
    upstream.courses[9] = []
    upstream.gate = asyncio.Event()
    tasks = [asyncio.create_task(cache.fetch_course_contents(c)) for c in (8, 9, 8, 9)]
    await _let_tasks_run()
    assert upstream.calls == 2
    upstream.gate.set()
    await asyncio.gather(*tasks)


async def test_concurrent_callers_share_a_failure_too(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient
) -> None:
    """An outage must not turn N waiters into N slow, sequential retries."""
    upstream.error = MoodleUnavailableError("down")
    upstream.gate = asyncio.Event()
    tasks = [asyncio.create_task(cache.fetch_course_contents(8)) for _ in range(10)]
    await _let_tasks_run()
    upstream.gate.set()

    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert upstream.calls == 1
    assert all(isinstance(r, MoodleUnavailableError) for r in results)


async def test_cancelled_caller_does_not_cancel_the_shared_load(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient
) -> None:
    upstream.gate = asyncio.Event()
    impatient = asyncio.create_task(cache.fetch_course_contents(8))
    patient = asyncio.create_task(cache.fetch_course_contents(8))
    await _let_tasks_run()

    impatient.cancel()
    await _let_tasks_run()
    upstream.gate.set()
    result = await patient

    assert impatient.cancelled()
    assert len(result.sections) == len(course8())
    assert upstream.calls == 1


async def test_stale_served_when_upstream_fails_within_max_age(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    await cache.fetch_course_contents(8)
    clock.advance(TTL + 1)
    upstream.error = MoodleUnavailableError("down")

    result = await cache.fetch_course_contents(8)

    assert result.stale is True
    assert len(result.sections) == len(course8())
    # And the plain interface serves it too (this is what /activities gets).
    assert len(await cache.get_course_contents(8)) == len(course8())


async def test_stale_covers_auth_and_unclassified_errors(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    await cache.fetch_course_contents(8)
    clock.advance(TTL + 1)
    for error in (MoodleAuthError("x"), MoodleError("x")):
        upstream.error = error
        assert (await cache.fetch_course_contents(8)).stale is True


async def test_stale_beyond_max_age_re_raises_the_typed_error(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    await cache.fetch_course_contents(8)
    clock.advance(STALE_MAX)  # at the limit: no longer "younger than"
    upstream.error = MoodleUnavailableError("down")

    with pytest.raises(MoodleUnavailableError):
        await cache.fetch_course_contents(8)


async def test_stale_age_counts_from_the_last_good_fetch(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    """Serving stale data must not refresh it, or it would never expire."""
    await cache.fetch_course_contents(8)
    upstream.error = MoodleUnavailableError("down")
    clock.advance(TTL + 1)
    assert (await cache.fetch_course_contents(8)).stale is True
    clock.advance(STALE_MAX - TTL - 2)  # still inside the limit
    assert (await cache.fetch_course_contents(8)).stale is True
    clock.advance(10)  # now past it, measured from the original fetch
    with pytest.raises(MoodleUnavailableError):
        await cache.fetch_course_contents(8)


async def test_errors_are_not_cached(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient
) -> None:
    upstream.error = MoodleUnavailableError("down")
    with pytest.raises(MoodleUnavailableError):
        await cache.fetch_course_contents(8)
    with pytest.raises(MoodleUnavailableError):
        await cache.fetch_course_contents(8)
    assert upstream.calls == 2  # the failure was not remembered

    upstream.error = None
    result = await cache.fetch_course_contents(8)
    assert result.stale is False and upstream.calls == 3


async def test_recovery_replaces_the_stale_copy(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    await cache.fetch_course_contents(8)
    clock.advance(TTL + 1)
    upstream.error = MoodleUnavailableError("down")
    assert (await cache.fetch_course_contents(8)).stale is True

    upstream.error = None
    assert (await cache.fetch_course_contents(8)).stale is False
    upstream.error = MoodleUnavailableError("down again")
    assert (await cache.fetch_course_contents(8)).stale is False  # fresh hit


async def test_course_not_found_is_definitive_and_never_served_stale(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    await cache.fetch_course_contents(8)
    clock.advance(TTL + 1)
    del upstream.courses[8]  # the fake raises MoodleCourseNotFoundError

    with pytest.raises(MoodleCourseNotFoundError):
        await cache.fetch_course_contents(8)
    # The entry was dropped, so a later outage cannot resurrect it.
    upstream.error = MoodleUnavailableError("down")
    with pytest.raises(MoodleUnavailableError):
        await cache.fetch_course_contents(8)


async def test_non_moodle_errors_propagate_and_are_not_masked_by_stale(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    await cache.fetch_course_contents(8)
    clock.advance(TTL + 1)
    upstream.error = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        await cache.fetch_course_contents(8)


async def test_entries_past_the_stale_limit_are_evicted(
    cache: CachedMoodleClient, upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    upstream.courses[9] = []
    await cache.fetch_course_contents(8)
    clock.advance(STALE_MAX + 1)
    await cache.fetch_course_contents(9)
    assert set(cache._entries) == {9}


async def test_zero_ttl_disables_caching(
    upstream: InMemoryMoodleClient, clock: FakeClock
) -> None:
    cache = CachedMoodleClient(
        upstream, ttl_seconds=0, stale_max_seconds=STALE_MAX, clock=clock
    )
    await cache.fetch_course_contents(8)
    await cache.fetch_course_contents(8)
    assert upstream.calls == 2


# --- logging ---------------------------------------------------------------


async def test_unavailable_logs_a_warning_without_message_text(
    cache: CachedMoodleClient,
    upstream: InMemoryMoodleClient,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await cache.fetch_course_contents(8)
    clock.advance(TTL + 1)
    upstream.error = MoodleUnavailableError("token=SECRET123 body={internal}")

    with caplog.at_level(logging.DEBUG, logger="app.moodle.cache"):
        await cache.fetch_course_contents(8)

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "get_course_contents" in message
    assert "MoodleUnavailableError" in message
    assert "outcome=stale-served" in message
    assert "elapsed=" in message
    assert "SECRET123" not in message and "internal" not in message
    assert "SECRET123" not in caplog.text


async def test_auth_error_logs_at_error_level_and_failed_outcome(
    cache: CachedMoodleClient,
    upstream: InMemoryMoodleClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upstream.error = MoodleAuthError("token=SECRET123")

    with (
        caplog.at_level(logging.DEBUG, logger="app.moodle.cache"),
        pytest.raises(MoodleAuthError),
    ):
        await cache.fetch_course_contents(8)

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert "MoodleAuthError" in record.getMessage()
    assert "outcome=failed" in record.getMessage()
    assert "SECRET123" not in caplog.text


async def test_auth_error_is_error_level_even_when_stale_is_served(
    cache: CachedMoodleClient,
    upstream: InMemoryMoodleClient,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An expired token is an operator problem even if students don't notice."""
    await cache.fetch_course_contents(8)
    clock.advance(TTL + 1)
    upstream.error = MoodleAuthError("x")

    with caplog.at_level(logging.DEBUG, logger="app.moodle.cache"):
        await cache.fetch_course_contents(8)

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert "outcome=stale-served" in record.getMessage()


async def test_elapsed_is_measured_with_the_clock(
    cache: CachedMoodleClient,
    upstream: InMemoryMoodleClient,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upstream.gate = asyncio.Event()
    upstream.error = MoodleUnavailableError("slow")
    task = asyncio.create_task(cache.fetch_course_contents(8))
    await _let_tasks_run()
    clock.advance(12.5)
    with (
        caplog.at_level(logging.DEBUG, logger="app.moodle.cache"),
        pytest.raises(MoodleUnavailableError),
    ):
        upstream.gate.set()
        await task
    assert "elapsed=12.50s" in caplog.text
