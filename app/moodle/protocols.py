"""Interface the service layer depends on for talking to Moodle.

Structural `Protocol`, like the repositories: implementations need no
inheritance, and callers never see httpx or Moodle's JSON.

Failures are reported with the exceptions in `app.moodle.exceptions`.
"""

from dataclasses import dataclass
from typing import Protocol

from app.moodle.schemas import MoodleSection, MoodleSiteInfo


@dataclass(frozen=True, slots=True)
class CourseContents:
    """Course sections plus how fresh they are.

    `stale` is True only when Moodle failed and an older copy was served in
    its place; any other answer (a live call, a cache hit inside the TTL) is
    fresh.
    """

    sections: list[MoodleSection]
    stale: bool


class CourseContentsProvider(Protocol):
    """Course contents together with their freshness.

    A separate, narrow Protocol on purpose: `MoodleClient.get_course_contents`
    keeps returning plain sections, so the HTTP client, the fakes and every
    caller that does not care about freshness (`/activities`) stay untouched.
    Only the callers that report `moodleStatus` depend on this one, and the
    cache decorator is what implements it. Raises the same `Moodle*Error`s.
    """

    async def fetch_course_contents(self, course_id: int) -> CourseContents: ...


class MoodleClient(Protocol):
    async def get_site_info(self) -> MoodleSiteInfo:
        """Connectivity/credential check."""
        ...

    async def get_course_contents(self, course_id: int) -> list[MoodleSection]:
        """Raises `MoodleCourseNotFoundError` if the course doesn't exist."""
        ...
