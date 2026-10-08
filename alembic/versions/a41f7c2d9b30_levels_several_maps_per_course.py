"""levels: several maps per course

- course_maps: the unique index on moodle_course_id becomes a plain index, and
  `position` and `moodle_section_id` are added (a section can have one map per
  course, enforced by a partial unique index).
- bubbles: `moodle_course_id` is copied from the parent map (NOT NULL) and the
  "one bubble per activity" rule moves from per-map to per-course. A composite
  foreign key (course_map_id, moodle_course_id) keeps the copy consistent.

Existing rows keep working: every course had exactly one map, so positions are
0 and the per-course uniqueness already holds.

Revision ID: a41f7c2d9b30
Revises: c2e8f4a61d07
Create Date: 2026-10-08 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a41f7c2d9b30"
down_revision: Union[str, Sequence[str], None] = "c2e8f4a61d07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_BUBBLE_UNIQUE = "uq_bubbles_course_map_id_activity_id"
NEW_BUBBLE_UNIQUE = "uq_bubbles_moodle_course_id_activity_id"
SECTION_INDEX = "uq_course_maps_course_section"
MAP_COURSE_INDEX = "ix_course_maps_moodle_course_id"
MAP_ID_COURSE_UNIQUE = "uq_course_maps_id_course"
BUBBLE_MAP_FK = "fk_bubbles_course_map"
OLD_BUBBLE_MAP_FK = "bubbles_course_map_id_fkey"


def upgrade() -> None:
    op.drop_index(MAP_COURSE_INDEX, table_name="course_maps")
    op.create_index(MAP_COURSE_INDEX, "course_maps", ["moodle_course_id"], unique=False)
    op.add_column(
        "course_maps",
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "course_maps", sa.Column("moodle_section_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        SECTION_INDEX,
        "course_maps",
        ["moodle_course_id", "moodle_section_id"],
        unique=True,
        postgresql_where=sa.text("moodle_section_id IS NOT NULL"),
    )
    op.create_unique_constraint(
        MAP_ID_COURSE_UNIQUE, "course_maps", ["id", "moodle_course_id"]
    )

    op.add_column("bubbles", sa.Column("moodle_course_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE bubbles SET moodle_course_id = course_maps.moodle_course_id"
        " FROM course_maps WHERE course_maps.id = bubbles.course_map_id"
    )
    op.alter_column("bubbles", "moodle_course_id", nullable=False)
    op.drop_constraint(OLD_BUBBLE_UNIQUE, "bubbles", type_="unique")
    op.create_unique_constraint(
        NEW_BUBBLE_UNIQUE, "bubbles", ["moodle_course_id", "activity_id"]
    )
    op.drop_constraint(OLD_BUBBLE_MAP_FK, "bubbles", type_="foreignkey")
    op.create_foreign_key(
        BUBBLE_MAP_FK,
        "bubbles",
        "course_maps",
        ["course_map_id", "moodle_course_id"],
        ["id", "moodle_course_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    crowded = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT moodle_course_id, count(*) AS n FROM course_maps"
                " GROUP BY moodle_course_id HAVING count(*) > 1"
                " ORDER BY moodle_course_id"
            )
        )
        .all()
    )
    if crowded:
        lines = [f"  course {row.moodle_course_id}: {row.n} maps" for row in crowded]
        raise RuntimeError(
            "Cannot go back to one map per course: some courses have several "
            "maps. Nothing was changed or deleted. Delete the extra maps, then "
            "run the downgrade again.\n" + "\n".join(lines)
        )
    op.drop_constraint(BUBBLE_MAP_FK, "bubbles", type_="foreignkey")
    op.create_foreign_key(
        OLD_BUBBLE_MAP_FK,
        "bubbles",
        "course_maps",
        ["course_map_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(NEW_BUBBLE_UNIQUE, "bubbles", type_="unique")
    op.create_unique_constraint(
        OLD_BUBBLE_UNIQUE, "bubbles", ["course_map_id", "activity_id"]
    )
    op.drop_column("bubbles", "moodle_course_id")

    op.drop_constraint(MAP_ID_COURSE_UNIQUE, "course_maps", type_="unique")
    op.drop_index(SECTION_INDEX, table_name="course_maps")
    op.drop_column("course_maps", "moodle_section_id")
    op.drop_column("course_maps", "position")
    op.drop_index(MAP_COURSE_INDEX, table_name="course_maps")
    op.create_index(MAP_COURSE_INDEX, "course_maps", ["moodle_course_id"], unique=True)
