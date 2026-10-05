"""Pydantic models for Moodle Web Services responses.

These describe *Moodle's* wire format, not our API: they are separate from
`app/schemas/`. Only the fields we use are declared; everything else is
ignored, and optional fields default to `None` because responses vary across
Moodle versions and installed plugins.
"""

from pydantic import BaseModel, ConfigDict, Field


class _MoodleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MoodleSiteInfo(_MoodleModel):
    """Subset of `core_webservice_get_site_info`."""

    sitename: str = ""
    username: str = ""
    userid: int | None = None
    release: str | None = None


class MoodleModule(_MoodleModel):
    """One activity/resource inside a course section."""

    id: int
    name: str
    modname: str  # quiz, assign, url, resource, forum, label, ...
    url: str | None = None
    # `visible`: 1 unless hidden by the teacher. `uservisible`: whether the
    # token's user can actually see it (also accounts for restrictions).
    visible: int | None = None
    uservisible: bool | None = None
    visibleoncoursepage: int | None = None
    # Completion tracking configuration (0 none, 1 manual, 2 automatic). This
    # is the activity's setting, not any user's completion state.
    completion: int | None = None


class MoodleSection(_MoodleModel):
    id: int
    name: str = ""
    section: int | None = None  # position within the course (0 = general)
    visible: int | None = None
    modules: list[MoodleModule] = Field(default_factory=list)
