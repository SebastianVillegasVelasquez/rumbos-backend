import uuid

from app.schemas.base import ApiModel


class ActivityRead(ApiModel):
    """A Moodle activity a bubble can point to, for the map editor's sidebar."""

    activity_id: int  # Moodle module id; what `Bubble.activity_id` references
    name: str
    modname: str  # quiz, scorm, assign, url, resource, ...
    url: str  # Moodle URL, to open the activity
    section_id: int  # Moodle section id; a different number space from activityId
    section_name: str
    section_number: int
    hidden: bool  # learners would not see it (hidden section/module)
    # Placed on ANY map of the course (an activity has one bubble per course).
    placed: bool
    bubble_id: uuid.UUID | None  # the bubble already referencing it, if any
    placed_in_map_id: uuid.UUID | None  # the map that bubble is on
