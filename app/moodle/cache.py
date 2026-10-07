"""Caching decorator for Moodle course contents.

`CachedMoodleClient` wraps any `MoodleClient` and caches the unfiltered parsed
sections of `get_course_contents`. Hidden/candidate filtering happens after the
cache (in the services), so those rules can change without invalidating
anything.

IMPORTANT: the cache lives in this process's memory. With several workers
(e.g. `uvicorn --workers 4`) every worker has its own copy, so Moodle can be
hit once per worker per TTL, and two workers may briefly serve different data.
That is acceptable for now; a shared cache (Redis) is a later decision.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.moodle.exceptions import (
    MoodleCourseNotFoundError,
    MoodleError,
    MoodleUnavailableError,
)
from app.moodle.protocols import CourseContents, MoodleClient
from app.moodle.schemas import MoodleSection, MoodleSiteInfo

logger = logging.getLogger(__name__)

_FUNCTION = "get_course_contents"

Clock = Callable[[], float]


@dataclass(slots=True)
class _Entry:
    sections: list[MoodleSection]
    fetched_at: (
        float  # clock reading when Moodle answered; never refreshed by stale hits
    )


class CachedMoodleClient:
    """TTL cache + single-flight + stale-on-error around a `MoodleClient`.

    Satisfies both `MoodleClient` (so `/activities` uses it unchanged) and
    `CourseContentsProvider` (so `/resolved` can tell fresh from stale).

    - Fresh: an entry younger than `ttl_seconds` is served without calling
      Moodle.
    - Single-flight: while a load for a course is running, every other caller
      for that course awaits the same load (its result *or its failure*), so a
      burst of students costs one Moodle call, also during an outage.
    - Stale-on-error: if the load fails and an entry younger than
      `stale_max_seconds` exists, it is served with `stale=True`. Otherwise the
      typed Moodle exception is re-raised. Errors are never cached.
    - A "course not found" answer is a definitive answer, not an outage: it
      drops the entry and is never served stale.

    Callers must treat the returned sections as read-only (the list is copied
    per call, the section objects are shared).
    """

    def __init__(
        self,
        upstream: MoodleClient,
        *,
        ttl_seconds: float,
        stale_max_seconds: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self._upstream = upstream
        self._ttl = ttl_seconds
        self._stale_max = stale_max_seconds
        self._clock = clock
        self._entries: dict[int, _Entry] = {}
        self._inflight: dict[int, asyncio.Task[CourseContents]] = {}

    async def get_site_info(self) -> MoodleSiteInfo:
        # A connectivity check: caching it would defeat its purpose.
        return await self._upstream.get_site_info()

    async def get_course_contents(self, course_id: int) -> list[MoodleSection]:
        return (await self.fetch_course_contents(course_id)).sections

    async def fetch_course_contents(self, course_id: int) -> CourseContents:
        entry = self._entries.get(course_id)
        if entry is not None and self._age(entry) < self._ttl:
            return CourseContents(list(entry.sections), stale=False)

        task = self._inflight.get(course_id)
        if task is None:
            task = asyncio.ensure_future(self._load(course_id))
            self._inflight[course_id] = task
            task.add_done_callback(lambda t: self._finish(course_id, t))
        # Shielded: a caller that is cancelled (client disconnected) must not
        # cancel the load the other waiters are sharing.
        result = await asyncio.shield(task)
        return CourseContents(list(result.sections), stale=result.stale)

    def _age(self, entry: _Entry) -> float:
        return self._clock() - entry.fetched_at

    def _finish(self, course_id: int, task: asyncio.Task[CourseContents]) -> None:
        if self._inflight.get(course_id) is task:
            del self._inflight[course_id]
        if not task.cancelled():
            task.exception()  # mark retrieved: waiters re-raise it themselves

    async def _load(self, course_id: int) -> CourseContents:
        started = self._clock()
        try:
            sections = await self._upstream.get_course_contents(course_id)
        except MoodleCourseNotFoundError as exc:
            self._entries.pop(course_id, None)
            self._log_failure(exc, started, "failed")
            raise
        except MoodleError as exc:
            entry = self._entries.get(course_id)
            if entry is not None and self._age(entry) < self._stale_max:
                self._log_failure(exc, started, "stale-served")
                return CourseContents(entry.sections, stale=True)
            self._log_failure(exc, started, "failed")
            raise
        self._store(course_id, sections)
        return CourseContents(sections, stale=False)

    def _store(self, course_id: int, sections: list[MoodleSection]) -> None:
        now = self._clock()
        self._entries[course_id] = _Entry(sections, now)
        # Entries past the stale limit can never be served again.
        for key in [
            k for k, e in self._entries.items() if self._age(e) >= self._stale_max
        ]:
            del self._entries[key]

    def _log_failure(self, exc: MoodleError, started: float, outcome: str) -> None:
        # Never the token, Moodle's response body or the exception's message:
        # only the function, the exception type, the time spent and the outcome.
        expected = isinstance(exc, MoodleUnavailableError | MoodleCourseNotFoundError)
        logger.log(
            logging.WARNING if expected else logging.ERROR,
            "Moodle %s failed: error=%s elapsed=%.2fs outcome=%s",
            _FUNCTION,
            type(exc).__name__,
            self._clock() - started,
            outcome,
        )
