import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.bubble import BubbleRead


class CourseMapCreate(BaseModel):
    moodle_course_id: int
    image_url: str


class CourseMapRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    moodle_course_id: int
    image_url: str
    created_at: datetime
    updated_at: datetime


class CourseMapDetail(CourseMapRead):
    """A course map together with its bubbles."""

    bubbles: list[BubbleRead]
