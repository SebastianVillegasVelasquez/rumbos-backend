"""skins, bubble sequence and per-map skin rules

- `skins` (with exactly one default, enforced by a partial unique index) and
  four built-in procedural skins with fixed ids.
- `course_maps.default_skin_id` and `bubbles.skin_id`, both cleared when their
  skin is deleted.
- `bubbles.sequence`, backfilled 0..n-1 per map by creation order.
- `course_map_skin_rules`: per map, which skin an activity type (modname) gets.

The seed data is written out here on purpose: a migration must keep meaning
what it meant when it was written, whatever the app code does later.

Revision ID: d5b8a31c7e90
Revises: b7d3e9a15c42
Create Date: 2026-10-08 14:00:00.000000

"""
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd5b8a31c7e90'
down_revision: Union[str, Sequence[str], None] = 'b7d3e9a15c42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Fixed ids: other data (and tests) may refer to the built-in skins.
ORBE_ID = '01900000-0000-7000-8000-000000000001'
INSIGNIA_ID = '01900000-0000-7000-8000-000000000002'
PIN_ID = '01900000-0000-7000-8000-000000000003'
HEXAGONO_ID = '01900000-0000-7000-8000-000000000004'

PALETTE = {
    'locked': {'fill': '#8A94A6', 'accent': '#5B6577', 'glow': '#B4BCCB'},
    'available': {'fill': '#FFB703', 'accent': '#E08E00', 'glow': '#FFD866'},
    'inProgress': {'fill': '#0E9AA7', 'accent': '#087680', 'glow': '#5ED3DD'},
    'complete': {'fill': '#2FBF71', 'accent': '#1E8E52', 'glow': '#7DE3A8'},
}


def _config(shape: str, idle: str, ring: bool, glow: bool) -> dict[str, Any]:
    return {
        'schemaVersion': 1,
        'kind': 'procedural',
        'shape': shape,
        'size': 56,
        'palette': PALETTE,
        'icon': {'mode': 'auto', 'preset': None, 'color': 'auto'},
        'label': {'mode': 'hover'},
        'effects': {'idle': idle, 'ring': ring, 'glow': glow, 'completion': 'burst'},
    }


# (id, name, is_default, config)
BUILTIN_SKINS = [
    (ORBE_ID, 'Orbe', True, _config('circle', 'breathe', ring=True, glow=True)),
    (INSIGNIA_ID, 'Insignia', False, _config('badge', 'none', ring=True, glow=False)),
    (PIN_ID, 'Pin', False, _config('pin', 'float', ring=False, glow=True)),
    (HEXAGONO_ID, 'Hexágono', False, _config('hexagon', 'pulse', ring=True, glow=True)),
]


def upgrade() -> None:
    skins = op.create_table(
        'skins',
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('is_builtin', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('is_default', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_skins_single_default',
        'skins',
        ['is_default'],
        unique=True,
        postgresql_where=sa.text('is_default'),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        skins,
        [
            {
                'id': uuid.UUID(skin_id),
                'name': name,
                'kind': 'procedural',
                'config': config,
                'is_builtin': True,
                'is_default': is_default,
                'created_at': now,
                'updated_at': now,
            }
            for skin_id, name, is_default, config in BUILTIN_SKINS
        ],
    )

    op.add_column('course_maps', sa.Column('default_skin_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_course_maps_default_skin_id_skins',
        'course_maps',
        'skins',
        ['default_skin_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index('ix_course_maps_default_skin_id', 'course_maps', ['default_skin_id'])

    op.add_column('bubbles', sa.Column('skin_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_bubbles_skin_id_skins',
        'bubbles',
        'skins',
        ['skin_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index('ix_bubbles_skin_id', 'bubbles', ['skin_id'])

    op.add_column('bubbles', sa.Column('sequence', sa.Integer(), nullable=True))
    op.execute(
        'UPDATE bubbles SET sequence = ranked.position FROM ('
        ' SELECT id, row_number() OVER ('
        '  PARTITION BY course_map_id ORDER BY created_at, id) - 1 AS position'
        ' FROM bubbles) AS ranked WHERE ranked.id = bubbles.id'
    )
    op.alter_column(
        'bubbles', 'sequence', nullable=False, server_default='0'
    )

    op.create_table(
        'course_map_skin_rules',
        sa.Column('course_map_id', sa.Uuid(), nullable=False),
        sa.Column('modname', sa.String(length=50), nullable=False),
        sa.Column('skin_id', sa.Uuid(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['course_map_id'], ['course_maps.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['skin_id'], ['skins.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'course_map_id', 'modname', name='uq_course_map_skin_rules_map_modname'
        ),
    )
    op.create_index(
        'ix_course_map_skin_rules_skin_id', 'course_map_skin_rules', ['skin_id']
    )


def downgrade() -> None:
    op.drop_index('ix_course_map_skin_rules_skin_id', table_name='course_map_skin_rules')
    op.drop_table('course_map_skin_rules')
    op.drop_column('bubbles', 'sequence')
    op.drop_index('ix_bubbles_skin_id', table_name='bubbles')
    op.drop_constraint('fk_bubbles_skin_id_skins', 'bubbles', type_='foreignkey')
    op.drop_column('bubbles', 'skin_id')
    op.drop_index('ix_course_maps_default_skin_id', table_name='course_maps')
    op.drop_constraint(
        'fk_course_maps_default_skin_id_skins', 'course_maps', type_='foreignkey'
    )
    op.drop_column('course_maps', 'default_skin_id')
    op.drop_index('uq_skins_single_default', table_name='skins')
    op.drop_table('skins')
