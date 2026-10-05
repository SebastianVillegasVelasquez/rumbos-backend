import uuid

from app.schemas.base import ApiModel


class ActivityRead(ApiModel):
    """A Moodle activity a bubble can point to, for the map editor's sidebar."""

    activity_id: int  # Moodle module id; what `Bubble.activity_id` references
    name: str
    modname: str  # quiz, assign, url, resource, forum, ...
    section_name: str
    placed: bool
    bubble_id: uuid.UUID | None  # the bubble already referencing it, if any
