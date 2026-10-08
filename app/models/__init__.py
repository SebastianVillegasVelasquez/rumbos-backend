"""Import every model here so its table registers on `BaseORM.metadata`.

Alembic's autogenerate only sees tables that have been imported.
"""

from app.models.asset import Asset
from app.models.base import BaseORM
from app.models.bubble import Bubble
from app.models.course_map import CourseMap
from app.models.skin import CourseMapSkinRule, Skin

__all__ = [
    "Asset",
    "BaseORM",
    "Bubble",
    "CourseMap",
    "CourseMapSkinRule",
    "Skin",
]
