"""tenant scoping

Maps, bubbles, assets and custom skins belong to a tenant. Every existing row
is attached to the "legacy" tenant (the built-in skins stay with nobody:
`tenant_id` NULL, visible to all). The uniqueness rules that were global
become per tenant, and a bubble's foreign key to its map now also pins the
tenant, so a bubble can never sit on another tenant's map.

Downgrading drops the tenant from those rules again; it fails if two tenants
by then hold clashing rows (the same activity in the same course id, say).

Revision ID: f6c3d9a5b247
Revises: e5b2c8f4a136
Create Date: 2026-10-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6c3d9a5b247'
down_revision: Union[str, Sequence[str], None] = 'e5b2c8f4a136'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors app.models.tenant; copied so the migration never changes with the app.
LEGACY_TENANT_ID = '00000000-0000-7000-8000-000000000001'


def upgrade() -> None:
    for table in ('course_maps', 'bubbles', 'assets', 'skins'):
        op.add_column(table, sa.Column('tenant_id', sa.Uuid(), nullable=True))

    for table in ('course_maps', 'bubbles', 'assets'):
        op.execute(sa.text(f'UPDATE {table} SET tenant_id = :t').bindparams(
            sa.bindparam('t', LEGACY_TENANT_ID, type_=sa.Uuid())
        ))
    op.execute(
        sa.text('UPDATE skins SET tenant_id = :t WHERE NOT is_builtin').bindparams(
            sa.bindparam('t', LEGACY_TENANT_ID, type_=sa.Uuid())
        )
    )

    for table in ('course_maps', 'bubbles', 'assets'):
        op.alter_column(table, 'tenant_id', nullable=False)
    for table in ('course_maps', 'bubbles', 'assets', 'skins'):
        op.create_foreign_key(
            f'fk_{table}_tenant_id_tenants', table, 'tenants', ['tenant_id'], ['id']
        )
        op.create_index(op.f(f'ix_{table}_tenant_id'), table, ['tenant_id'])

    # Course maps: one per section per (tenant, course); bubbles pin the tenant.
    op.drop_constraint('fk_bubbles_course_map', 'bubbles', type_='foreignkey')
    op.drop_constraint('uq_course_maps_id_course', 'course_maps', type_='unique')
    op.create_unique_constraint(
        'uq_course_maps_id_tenant_course',
        'course_maps',
        ['id', 'tenant_id', 'moodle_course_id'],
    )
    op.drop_index('uq_course_maps_course_section', table_name='course_maps')
    op.create_index(
        'uq_course_maps_course_section',
        'course_maps',
        ['tenant_id', 'moodle_course_id', 'moodle_section_id'],
        unique=True,
        postgresql_where=sa.text('moodle_section_id IS NOT NULL'),
    )
    op.create_foreign_key(
        'fk_bubbles_course_map',
        'bubbles',
        'course_maps',
        ['course_map_id', 'tenant_id', 'moodle_course_id'],
        ['id', 'tenant_id', 'moodle_course_id'],
        ondelete='CASCADE',
    )
    op.drop_constraint(
        'uq_bubbles_moodle_course_id_activity_id', 'bubbles', type_='unique'
    )
    op.create_unique_constraint(
        'uq_bubbles_moodle_course_id_activity_id',
        'bubbles',
        ['tenant_id', 'moodle_course_id', 'activity_id'],
    )

    op.drop_constraint('uq_assets_kind_sha256', 'assets', type_='unique')
    op.create_unique_constraint(
        'uq_assets_kind_sha256', 'assets', ['tenant_id', 'kind', 'sha256']
    )


def downgrade() -> None:
    op.drop_constraint('uq_assets_kind_sha256', 'assets', type_='unique')
    op.create_unique_constraint('uq_assets_kind_sha256', 'assets', ['kind', 'sha256'])

    op.drop_constraint(
        'uq_bubbles_moodle_course_id_activity_id', 'bubbles', type_='unique'
    )
    op.create_unique_constraint(
        'uq_bubbles_moodle_course_id_activity_id',
        'bubbles',
        ['moodle_course_id', 'activity_id'],
    )
    op.drop_constraint('fk_bubbles_course_map', 'bubbles', type_='foreignkey')
    op.drop_index('uq_course_maps_course_section', table_name='course_maps')
    op.create_index(
        'uq_course_maps_course_section',
        'course_maps',
        ['moodle_course_id', 'moodle_section_id'],
        unique=True,
        postgresql_where=sa.text('moodle_section_id IS NOT NULL'),
    )
    op.drop_constraint('uq_course_maps_id_tenant_course', 'course_maps', type_='unique')
    op.create_unique_constraint(
        'uq_course_maps_id_course', 'course_maps', ['id', 'moodle_course_id']
    )
    op.create_foreign_key(
        'fk_bubbles_course_map',
        'bubbles',
        'course_maps',
        ['course_map_id', 'moodle_course_id'],
        ['id', 'moodle_course_id'],
        ondelete='CASCADE',
    )

    for table in ('skins', 'assets', 'bubbles', 'course_maps'):
        op.drop_index(op.f(f'ix_{table}_tenant_id'), table_name=table)
        op.drop_constraint(f'fk_{table}_tenant_id_tenants', table, type_='foreignkey')
        op.drop_column(table, 'tenant_id')
