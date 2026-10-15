"""view3d

Per-map 3D view: `course_maps.view3d_scene` names the scene (NULL = no 3D view)
and `course_map_view3d_bindings` says which bubble sits in which zone, with
optional ES/EN text overrides. Existing maps start without a 3D view, so their
snapshots (which omit the key) and hashes are unchanged.

Revision ID: a7c4e1d90b35
Revises: c9d2e5f8a416
Create Date: 2026-10-15 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a7c4e1d90b35'
down_revision: Union[str, Sequence[str], None] = 'c9d2e5f8a416'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'course_maps', sa.Column('view3d_scene', sa.Text(), nullable=True)
    )
    op.create_table(
        'course_map_view3d_bindings',
        sa.Column('course_map_id', sa.Uuid(), nullable=False),
        sa.Column('zone_id', sa.Integer(), nullable=False),
        sa.Column('bubble_id', sa.Uuid(), nullable=False),
        sa.Column('title', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'description', postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            'zone_id >= 0', name='ck_course_map_view3d_bindings_zone'
        ),
        sa.ForeignKeyConstraint(
            ['course_map_id'], ['course_maps.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['bubble_id'], ['bubbles.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'course_map_id', 'zone_id', name='uq_course_map_view3d_bindings_map_zone'
        ),
        sa.UniqueConstraint(
            'course_map_id',
            'bubble_id',
            name='uq_course_map_view3d_bindings_map_bubble',
        ),
    )
    op.create_index(
        op.f('ix_course_map_view3d_bindings_bubble_id'),
        'course_map_view3d_bindings',
        ['bubble_id'],
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_course_map_view3d_bindings_bubble_id'),
        table_name='course_map_view3d_bindings',
    )
    op.drop_table('course_map_view3d_bindings')
    op.drop_column('course_maps', 'view3d_scene')
