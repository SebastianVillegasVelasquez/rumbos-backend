"""publications

Immutable snapshots of a map (what students see). A map's current publication
is its highest number. `asset_ids` lists the images a snapshot depends on, with
a GIN index so "is this image published anywhere?" is a cheap question.

Revision ID: b8e4c1a7d035
Revises: a3f6d2b8c914
Create Date: 2026-10-10 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b8e4c1a7d035'
down_revision: Union[str, Sequence[str], None] = 'a3f6d2b8c914'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'publications',
        sa.Column('course_map_id', sa.Uuid(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('content_hash', sa.String(), nullable=False),
        sa.Column(
            'asset_ids',
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['course_map_id'], ['course_maps.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'course_map_id', 'number', name='uq_publications_course_map_number'
        ),
    )
    op.create_index(
        'ix_publications_asset_ids',
        'publications',
        ['asset_ids'],
        postgresql_using='gin',
    )


def downgrade() -> None:
    op.drop_index('ix_publications_asset_ids', table_name='publications')
    op.drop_table('publications')
