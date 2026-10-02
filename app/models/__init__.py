"""Import every model here so its table registers on `BaseORM.metadata`.

Alembic's autogenerate only sees tables that have been imported.
"""

from app.models.base import BaseORM
from app.models.bubble import Bubble
from app.models.course_map import CourseMap

__all__ = ["BaseORM", "Bubble", "CourseMap"]
