"""Pydantic models for Moodle Web Services responses.

These describe *Moodle's* wire format, not our API: they are separate from
`app/schemas/`. Only the fields we use are declared; everything else is
ignored. In particular a section's `summary` (often ~20 KB of untrusted,
hand-written HTML) is deliberately not declared, so it is dropped at parse
time and can never be stored, logged or forwarded.

Shapes were checked against a real `core_course_get_contents` response
(`tests/fixtures/moodle_course_contents_course8.json`).
"""

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _to_bool(value: Any) -> Any:
    """Moodle sends visibility as 0/1 ints in some places and bools in others."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value != 0
    return value


# Accepts 0/1/bool; everything downstream sees a plain bool.
MoodleBool = Annotated[bool, BeforeValidator(_to_bool)]


class _MoodleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MoodleSiteInfo(_MoodleModel):
    """Subset of `core_webservice_get_site_info`."""

    sitename: str = ""
    username: str = ""
    userid: int | None = None
    release: str | None = None


class MoodleModule(_MoodleModel):
    """One activity/resource inside a course section.

    A missing visibility flag means visible: Moodle versions and plugins vary
    in which flags they send.
    """

    # The course-module id ("cmid", the number in `view.php?id=`). This is what
    # `Bubble.activity_id` refers to. It is NOT `instance`, and it lives in a
    # different namespace from section ids (the two can be equal).
    id: int
    # Id of the row in the activity's own table (e.g. the quiz id). Not used
    # to identify activities.
    instance: int | None = None
    name: str  # returned unchanged; may embed internal codes like "[VIAJE-1-1]"
    modname: str  # quiz, scorm, customcert, assign, url, resource, label, ...
    url: str | None = None
    visible: MoodleBool = True
    # Whether the *token's user* can see it. The service user is privileged,
    # so this says nothing about what a learner sees.
    uservisible: MoodleBool = True
    # False for "stealth" activities: reachable by link, not listed on the page.
    visibleoncoursepage: MoodleBool | None = None
    noviewlink: MoodleBool = False  # True: nothing to open (e.g. inline content)
    candisplay: MoodleBool = True
    # Completion tracking configuration (0 disabled, 1 manual, 2 automatic),
    # not any user's completion state.
    completion: int = 0
    # Raw access-restriction JSON string, or None. Kept as-is, not interpreted.
    availability: str | None = None


class MoodleSection(_MoodleModel):
    id: int  # section id; unrelated to module ids
    name: str = ""
    section: int | None = None  # position within the course (0 = general)
    # A hidden section hides all its modules from learners, whatever the
    # modules' own flags say.
    visible: MoodleBool = True
    uservisible: MoodleBool = True
    modules: list[MoodleModule] = Field(default_factory=list)
