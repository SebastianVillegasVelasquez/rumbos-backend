from typing import TYPE_CHECKING

from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseORM

if TYPE_CHECKING:
    from app.models.bubble import Bubble


class CourseMap(BaseORM):
    __tablename__ = "course_maps"

    # One map per Moodle course for now (unique); the unique index also
    # serves lookups by this column.
    moodle_course_id: Mapped[int] = mapped_column(unique=True, index=True)
    image_url: Mapped[str]

    # Deleting a map deletes its bubbles. `passive_deletes` lets the
    # database's ON DELETE CASCADE do it without loading the collection
    # (lazy loading is unavailable in async code); `lazy="raise"` makes any
    # accidental implicit load fail loudly. Query bubbles explicitly.
    bubbles: Mapped[list["Bubble"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
