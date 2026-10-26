"""app meta

Key/value facts about the installation. The first one is `storage_id`, which ties
the database to the asset storage directory it was filled from (see
`app.assets.fingerprint`). Nothing is written here by the migration itself.

Revision ID: d7a2f5c8b391
Revises: b6e1c8a4d273
Create Date: 2026-10-25 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7a2f5c8b391'
down_revision: Union[str, Sequence[str], None] = 'b6e1c8a4d273'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_meta',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('app_meta')
