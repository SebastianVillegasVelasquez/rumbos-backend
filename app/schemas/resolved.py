import uuid

from app.enums import Availability, MoodleStatus
from app.schemas.base import ApiModel


class ResolvedActivity(ApiModel):
    activity_id: int  # Moodle module id (cmid)
    name: str
    modname: str
    section_name: str
    section_number: int
    url: str


class ResolvedBubble(ApiModel):
    bubble_id: uuid.UUID
    availability: Availability
    # Null unless the activity is available, or hidden with includeHidden=true.
    activity: ResolvedActivity | None


class ResolvedMap(ApiModel):
    moodle_status: MoodleStatus
    # One entry per bubble of the map, in the order of `GET /course-maps/{id}`.
    bubbles: list[ResolvedBubble]
