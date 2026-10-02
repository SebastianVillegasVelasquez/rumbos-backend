import uuid
from datetime import datetime

from app.schemas.base import ApiModel
from app.schemas.bubble import BubbleRead


class CourseMapCreate(ApiModel):
    moodle_course_id: int
    image_url: str


class CourseMapRead(ApiModel):
    id: uuid.UUID
    moodle_course_id: int
    image_url: str
    created_at: datetime
    updated_at: datetime


class CourseMapDetail(CourseMapRead):
    """A course map together with its bubbles."""

    bubbles: list[BubbleRead]
