"""asset metadata

Editors browse a library of images, so assets get a title, an optional credit
and the name of the uploaded file. Existing rows get a title built from their
kind and upload date.

Revision ID: f1a9c3d7b2e4
Revises: e3c7a94d2b18
Create Date: 2026-10-10 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a9c3d7b2e4'
down_revision: Union[str, Sequence[str], None] = 'e3c7a94d2b18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('assets', sa.Column('title', sa.String(length=120), nullable=True))
    op.add_column('assets', sa.Column('credit', sa.String(length=300), nullable=True))
    op.add_column(
        'assets', sa.Column('original_filename', sa.String(length=255), nullable=True)
    )
    op.execute(
        """
        UPDATE assets SET title =
            CASE kind WHEN 'background' THEN 'Fondo' ELSE 'Imagen' END
            || ' ' || to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')
        """
    )
    op.alter_column('assets', 'title', nullable=False)


def downgrade() -> None:
    op.drop_column('assets', 'original_filename')
    op.drop_column('assets', 'credit')
    op.drop_column('assets', 'title')
