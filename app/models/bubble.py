import uuid

from sqlalchemy import CheckConstraint, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import BubbleIcon, BubbleStatus
from app.models.base import BaseORM


def _values(enum_cls: type[BubbleStatus] | type[BubbleIcon]) -> list[str]:
    return [member.value for member in enum_cls]


class Bubble(BaseORM):
    __tablename__ = "bubbles"
    __table_args__ = (
        CheckConstraint("x >= 0 AND x <= 1", name="ck_bubbles_x_range"),
        CheckConstraint("y >= 0 AND y <= 1", name="ck_bubbles_y_range"),
    )

    course_map_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_maps.id", ondelete="CASCADE"), index=True
    )
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
