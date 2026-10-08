import uuid

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import BubbleIcon, BubbleStatus
from app.models.base import BaseORM


def _values(enum_cls: type[BubbleStatus] | type[BubbleIcon]) -> list[str]:
    return [member.value for member in enum_cls]


# A Moodle activity appears at most once per COURSE (on one of its maps). The
# database enforces it (so concurrent requests cannot both win); the repository
# maps a violation of this constraint to `ActivityAlreadyPlacedError`.
UQ_BUBBLE_ACTIVITY = "uq_bubbles_moodle_course_id_activity_id"


class Bubble(BaseORM):
    __tablename__ = "bubbles"
    __table_args__ = (
        UniqueConstraint("moodle_course_id", "activity_id", name=UQ_BUBBLE_ACTIVITY),
        # Composite so the course id copied onto the bubble must match the
        # parent map's: it cannot drift, and the map's can't change under it.
        ForeignKeyConstraint(
            ["course_map_id", "moodle_course_id"],
            ["course_maps.id", "course_maps.moodle_course_id"],
            name="fk_bubbles_course_map",
            ondelete="CASCADE",
        ),
        CheckConstraint("x >= 0 AND x <= 1", name="ck_bubbles_x_range"),
        CheckConstraint("y >= 0 AND y <= 1", name="ck_bubbles_y_range"),
    )

    course_map_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # Copied from the parent map by the service on create; immutable. It is
    # what makes "one bubble per activity per course" a plain unique constraint.
    moodle_course_id: Mapped[int]
    # Overrides the map's rules and default. Deleting the skin clears it.
    skin_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("skins.id", ondelete="SET NULL"), index=True
    )
    # Position in the guided path (0-based). Only the order endpoint changes
    # it; new bubbles go last. Ties are tolerated and broken by `created_at, id`.
    sequence: Mapped[int] = mapped_column(server_default="0")
    # Opaque Moodle activity id; not a foreign key (Moodle data isn't here).
    activity_id: Mapped[int]
    # Relative position on a 0-1 scale, never pixels.
    x: Mapped[float]
    y: Mapped[float]
    # Stored as VARCHAR (not a native PG enum) so adding values never needs
    # an ALTER TYPE migration. NULL means "unset" (frontend shows "question").
    icon: Mapped[BubbleIcon | None] = mapped_column(
        Enum(
            BubbleIcon,
            native_enum=False,
            length=20,
            values_callable=_values,
            validate_strings=True,
        )
    )
    status: Mapped[BubbleStatus] = mapped_column(
        Enum(
            BubbleStatus,
            native_enum=False,
            length=20,
            values_callable=_values,
            validate_strings=True,
        ),
        default=BubbleStatus.LOCKED,
    )
