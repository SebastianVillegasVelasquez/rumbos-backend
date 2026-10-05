"""Interface the service layer depends on for talking to Moodle.

Structural `Protocol`, like the repositories: implementations need no
inheritance, and callers never see httpx or Moodle's JSON.

Failures are reported with the exceptions in `app.moodle.exceptions`.
"""

from typing import Protocol

from app.moodle.schemas import MoodleSection, MoodleSiteInfo


class MoodleClient(Protocol):
    async def get_site_info(self) -> MoodleSiteInfo:
        """Connectivity/credential check."""
        ...

    async def get_course_contents(self, course_id: int) -> list[MoodleSection]:
        """Raises `MoodleCourseNotFoundError` if the course doesn't exist."""
        ...
