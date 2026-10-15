import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseORM

# The repository maps a violation of these to the matching `View3dInvalidError`
# code; the service checks first, so they only fire if two writers race.
UQ_VIEW3D_ZONE = "uq_course_map_view3d_bindings_map_zone"
UQ_VIEW3D_BUBBLE = "uq_course_map_view3d_bindings_map_bubble"


class CourseMapView3dBinding(BaseORM):
    """In the map's 3D scene (`course_maps.view3d_scene`), zone `zone_id` shows
    this bubble. `title` / `description` are optional ES/EN overrides, stored as
    `{"es": ..., "en": ...}` with only the languages that were given."""

    __tablename__ = "course_map_view3d_bindings"
    __table_args__ = (
        UniqueConstraint("course_map_id", "zone_id", name=UQ_VIEW3D_ZONE),
        UniqueConstraint("course_map_id", "bubble_id", name=UQ_VIEW3D_BUBBLE),
        CheckConstraint("zone_id >= 0", name="ck_course_map_view3d_bindings_zone"),
    )

    course_map_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_maps.id", ondelete="CASCADE")
    )
    # Which zones exist depends on the scene, which lives in code (see
    # `app.view3d.scenes`), so the upper bound is checked by the service.
    zone_id: Mapped[int]
    # Deleting the bubble deletes its binding, in the same transaction.
    bubble_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bubbles.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    description: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
