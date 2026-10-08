import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseORM

if TYPE_CHECKING:
    from app.models.bubble import Bubble

# At most one map per Moodle section in a course. Partial: maps that are not
# tied to a section (NULL) can repeat. The repository maps a violation of this
# index to `SectionAlreadyMappedError`.
UQ_COURSE_MAP_SECTION = "uq_course_maps_course_section"


class CourseMap(BaseORM):
    __tablename__ = "course_maps"
    __table_args__ = (
        Index(
            UQ_COURSE_MAP_SECTION,
            "moodle_course_id",
            "moodle_section_id",
            unique=True,
            postgresql_where=text("moodle_section_id IS NOT NULL"),
        ),
        # Lets `bubbles` reference (id, moodle_course_id) so a bubble's course
        # can never disagree with its map's (see `Bubble`).
        UniqueConstraint("id", "moodle_course_id", name="uq_course_maps_id_course"),
    )

    title: Mapped[str] = mapped_column(String(120))
    # A course has several maps ("levels"); this index serves the lookups.
    # Never changes after creation: no update path exists, and bubbles pin it.
    moodle_course_id: Mapped[int] = mapped_column(index=True)
    # The Moodle section this level covers, if any. A section id, never a
    # module id (the two number spaces collide).
    moodle_section_id: Mapped[int | None]
    # Order among the course's maps. Duplicates are tolerated (concurrent
    # creates); lists break ties by `created_at, id`.
    position: Mapped[int] = mapped_column(server_default="0")
    image_url: Mapped[str]
    # `MapSettings` as camelCase JSON. Always read through that model, which
    # fills in whatever the stored object lacks (an empty `{}` is valid).
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    # Skin for bubbles with no skin of their own and no matching rule. Deleting
    # the skin clears it (the app then falls back to the default skin).
    default_skin_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("skins.id", ondelete="SET NULL"), index=True
    )

    # Deleting a map deletes its bubbles. `passive_deletes` lets the
    # database's ON DELETE CASCADE do it without loading the collection
    # (lazy loading is unavailable in async code); `lazy="raise"` makes any
    # accidental implicit load fail loudly. Query bubbles explicitly.
    bubbles: Mapped[list["Bubble"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
