"""audio assets

Assets can now be audio. An audio track has a duration instead of dimensions,
a credit, an optional licence text and the rights trail of its upload (the
pseudonymous uploader id and when the rights were declared). It can also be
quarantined, which takes it out of service without deleting it. An audio asset
with no tenant belongs to the library curated by the operator; images keep
requiring a tenant.

`asset_audit_events` records when an asset was created, quarantined or
released, and why.

Downgrading is refused while audio assets exist: there is nowhere to put them.

Revision ID: c4f8a2d6e913
Revises: f6c3d9a5b247
Create Date: 2026-10-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4f8a2d6e913'
down_revision: Union[str, Sequence[str], None] = 'f6c3d9a5b247'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors app.models.asset; copied so the migration never changes with the app.
KIND_FIELDS = (
    "(kind = 'audio' AND width IS NULL AND height IS NULL"
    " AND duration_seconds > 0 AND credit IS NOT NULL)"
    " OR (kind <> 'audio' AND width IS NOT NULL AND height IS NOT NULL"
    " AND duration_seconds IS NULL AND tenant_id IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column('assets', sa.Column('duration_seconds', sa.Float(), nullable=True))
    op.add_column('assets', sa.Column('license', sa.String(length=120), nullable=True))
    op.add_column(
        'assets', sa.Column('uploaded_by_sub', sa.String(length=32), nullable=True)
    )
    op.add_column(
        'assets',
        sa.Column('rights_declared_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'assets',
        sa.Column('quarantined_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('assets', sa.Column('quarantine_reason', sa.Text(), nullable=True))

    op.alter_column('assets', 'width', nullable=True)
    op.alter_column('assets', 'height', nullable=True)
    op.alter_column('assets', 'tenant_id', nullable=True)
    op.create_check_constraint('ck_assets_kind_fields', 'assets', KIND_FIELDS)
    op.create_index(
        'uq_assets_library_kind_sha256',
        'assets',
        ['kind', 'sha256'],
        unique=True,
        postgresql_where=sa.text('tenant_id IS NULL'),
    )

    op.create_table(
        'asset_audit_events',
        sa.Column('asset_id', sa.Uuid(), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_asset_audit_events_asset_id'), 'asset_audit_events', ['asset_id']
    )


def downgrade() -> None:
    audio = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM assets WHERE kind = 'audio'"))
        .scalar_one()
    )
    if audio:
        raise RuntimeError(
            f'{audio} audio asset(s) exist and cannot be kept without audio '
            'support; delete them (and the maps that use them) first'
        )
    op.drop_index(
        op.f('ix_asset_audit_events_asset_id'), table_name='asset_audit_events'
    )
    op.drop_table('asset_audit_events')
    op.drop_index('uq_assets_library_kind_sha256', table_name='assets')
    op.drop_constraint('ck_assets_kind_fields', 'assets', type_='check')
    op.alter_column('assets', 'tenant_id', nullable=False)
    op.alter_column('assets', 'height', nullable=False)
    op.alter_column('assets', 'width', nullable=False)
    for column in (
        'quarantine_reason',
        'quarantined_at',
        'rights_declared_at',
        'uploaded_by_sub',
        'license',
        'duration_seconds',
    ):
        op.drop_column('assets', column)
