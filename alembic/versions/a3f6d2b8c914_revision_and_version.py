"""optimistic concurrency counters

`course_maps.revision` and `bubbles.version` start at 1 and are bumped by every
guarded write, so two editors working from the same state cannot silently
overwrite each other.

Revision ID: a3f6d2b8c914
Revises: f1a9c3d7b2e4
Create Date: 2026-10-10 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f6d2b8c914'
down_revision: Union[str, Sequence[str], None] = 'f1a9c3d7b2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'course_maps',
        sa.Column('revision', sa.Integer(), server_default='1', nullable=False),
    )
    op.add_column(
        'bubbles',
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('bubbles', 'version')
    op.drop_column('course_maps', 'revision')
