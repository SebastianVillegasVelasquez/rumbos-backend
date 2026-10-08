"""course map settings

`course_maps.settings` holds the presentation settings (mode, fit, path,
ambient effects...) as JSON. Existing maps get `{}`, which reads as the
defaults.

Revision ID: e3c7a94d2b18
Revises: d5b8a31c7e90
Create Date: 2026-10-08 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e3c7a94d2b18'
down_revision: Union[str, Sequence[str], None] = 'd5b8a31c7e90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'course_maps',
        sa.Column(
            'settings',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('course_maps', 'settings')
