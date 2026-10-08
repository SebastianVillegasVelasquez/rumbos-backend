"""add assets

Metadata of user-uploaded images (the files live in the asset storage). The
same bytes uploaded twice as the same kind are one row.

Revision ID: b7d3e9a15c42
Revises: a41f7c2d9b30
Create Date: 2026-10-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7d3e9a15c42'
down_revision: Union[str, Sequence[str], None] = 'a41f7c2d9b30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'assets',
        sa.Column(
            'kind',
            sa.Enum('background', 'bubble', name='assetkind', native_enum=False, length=20),
            nullable=False,
        ),
        sa.Column('mime', sa.String(length=32), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('width > 0 AND height > 0', name='ck_assets_dimensions'),
        sa.CheckConstraint('bytes > 0', name='ck_assets_bytes'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kind', 'sha256', name='uq_assets_kind_sha256'),
    )


def downgrade() -> None:
    op.drop_table('assets')
