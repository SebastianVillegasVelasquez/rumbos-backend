import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import BubbleIcon, BubbleKind, BubbleStatus
from app.models.base import BaseORM


def _values(
    enum_cls: type[BubbleStatus] | type[BubbleIcon] | type[BubbleKind],
) -> list[str]:
    return [member.value for member in enum_cls]


# A Moodle activity appears at most once per COURSE of a tenant (on one of its maps). The
# database enforces it (so concurrent requests cannot both win); the repository
# maps a violation of this index to `ActivityAlreadyPlacedError`. It is partial:
# content bubbles have no activity and are not part of it.
UQ_BUBBLE_ACTIVITY = "uq_bubbles_moodle_course_id_activity_id"
CK_BUBBLE_KIND_FIELDS = "ck_bubbles_kind_fields"


class Bubble(BaseORM):
    __tablename__ = "bubbles"
    __table_args__ = (
        Index(
            UQ_BUBBLE_ACTIVITY,
            "tenant_id",
            "moodle_course_id",
            "activity_id",
            unique=True,
            postgresql_where=text("kind = 'activity'"),
        ),
        # An activity bubble points at its Moodle module and has no content; a
        # content bubble is the other way round. Nothing in between.
        CheckConstraint(
            "(kind = 'activity' AND activity_id IS NOT NULL AND content IS NULL)"
            " OR (kind = 'content' AND activity_id IS NULL AND content IS NOT NULL)",
            name=CK_BUBBLE_KIND_FIELDS,
        ),
        # Composite so the tenant and course id copied onto the bubble must match
        # the parent map's: they cannot drift, and the map's can't change under it.
        ForeignKeyConstraint(
            ["course_map_id", "tenant_id", "moodle_course_id"],
            [
                "course_maps.id",
                "course_maps.tenant_id",
                "course_maps.moodle_course_id",
            ],
            name="fk_bubbles_course_map",
            ondelete="CASCADE",
        ),
        CheckConstraint("x >= 0 AND x <= 1", name="ck_bubbles_x_range"),
        CheckConstraint("y >= 0 AND y <= 1", name="ck_bubbles_y_range"),
    )

    course_map_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # Copied from the parent map by the scope on create; immutable.
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
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
    # None for a content bubble.
    activity_id: Mapped[int | None]
    # Stored as VARCHAR like the other enums. Immutable after creation.
    kind: Mapped[BubbleKind] = mapped_column(
        Enum(
            BubbleKind,
            native_enum=False,
            length=20,
            values_callable=_values,
            validate_strings=True,
        ),
        default=BubbleKind.ACTIVITY,
        server_default=BubbleKind.ACTIVITY.value,
    )
    # The validated content of a content bubble (see `app.schemas.content`), in
    # its stored camelCase form; belongs to this one bubble. None for an
    # activity bubble.
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # Optimistic concurrency token, like `CourseMap.revision`. Bumped by edits to
    # draft content (position, icon, skin) and NOT by `status`, which is live data.
    version: Mapped[int] = mapped_column(default=1, server_default="1")
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
