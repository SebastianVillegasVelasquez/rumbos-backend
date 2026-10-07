"""add course_maps.title

Adds the column nullable, backfills existing rows, then makes it NOT NULL, so
the migration works on a database that already has maps.

Revision ID: 3b7d1c9e5a42
Revises: ffbafbaf2f65
Create Date: 2026-10-07 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3b7d1c9e5a42'
down_revision: Union[str, Sequence[str], None] = 'ffbafbaf2f65'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('course_maps', sa.Column('title', sa.String(length=120), nullable=True))
    op.execute("UPDATE course_maps SET title = 'Mapa del curso ' || moodle_course_id")
    op.alter_column('course_maps', 'title', existing_type=sa.String(length=120), nullable=False)


def downgrade() -> None:
    op.drop_column('course_maps', 'title')
