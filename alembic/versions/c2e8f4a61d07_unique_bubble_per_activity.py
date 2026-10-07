"""one bubble per activity on a map

Adds UNIQUE (course_map_id, activity_id). Existing duplicates are never
deleted: if any exist the migration aborts and lists them, so someone can
decide which bubble to keep.

Revision ID: c2e8f4a61d07
Revises: 3b7d1c9e5a42
Create Date: 2026-10-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2e8f4a61d07'
down_revision: Union[str, Sequence[str], None] = '3b7d1c9e5a42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT = 'uq_bubbles_course_map_id_activity_id'


def upgrade() -> None:
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT course_map_id, activity_id, count(*) AS n,"
            " string_agg(id::text, ', ' ORDER BY id) AS bubble_ids"
            " FROM bubbles GROUP BY course_map_id, activity_id"
            " HAVING count(*) > 1 ORDER BY course_map_id, activity_id"
        )
    ).all()
    if duplicates:
        lines = [
            f"  map {row.course_map_id}, activity {row.activity_id}: "
            f"{row.n} bubbles ({row.bubble_ids})"
            for row in duplicates
        ]
        raise RuntimeError(
            "Cannot add the unique constraint on bubbles (course_map_id, "
            "activity_id): duplicates exist. Nothing was changed or deleted. "
            "Delete the extra bubbles, then run the migration again.\n"
            + "\n".join(lines)
        )
    op.create_unique_constraint(CONSTRAINT, 'bubbles', ['course_map_id', 'activity_id'])


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, 'bubbles', type_='unique')
