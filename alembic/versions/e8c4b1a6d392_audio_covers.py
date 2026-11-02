"""audio covers

An audio track can have a cover: a square WebP asset of its own (kind
'cover'), made from an uploaded picture or from the artwork found inside the
audio file. `assets.cover_asset_id` points at it. Several tracks may share one
cover (an album), so the cover is only deleted when no track points at it.

A cover has dimensions like an image, but it has no tenant of its own when its
track is a library track, so the kind check lets 'cover' through without one.

Nothing existing changes: every current row keeps satisfying the new checks.
Downgrading is refused while covers exist, since the older checks have no
place for a global image.

Revision ID: e8c4b1a6d392
Revises: d7a2f5c8b391
Create Date: 2026-11-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e8c4b1a6d392'
down_revision: Union[str, Sequence[str], None] = 'd7a2f5c8b391'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors app.models.asset; copied so the migration never changes with the app.
KIND_FIELDS = (
    "(kind = 'audio' AND width IS NULL AND height IS NULL"
    " AND duration_seconds > 0 AND credit IS NOT NULL)"
    " OR (kind = 'cover' AND width IS NOT NULL AND height IS NOT NULL"
    " AND duration_seconds IS NULL)"
    " OR (kind NOT IN ('audio', 'cover') AND width IS NOT NULL"
    " AND height IS NOT NULL AND duration_seconds IS NULL"
    " AND tenant_id IS NOT NULL)"
)
COVER_LINK = "cover_asset_id IS NULL OR (kind = 'audio' AND cover_asset_id <> id)"
# The check as it was before covers.
OLD_KIND_FIELDS = (
    "(kind = 'audio' AND width IS NULL AND height IS NULL"
    " AND duration_seconds > 0 AND credit IS NOT NULL)"
    " OR (kind <> 'audio' AND width IS NOT NULL AND height IS NOT NULL"
    " AND duration_seconds IS NULL AND tenant_id IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column(
        'assets',
        sa.Column('cover_asset_id', sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        'fk_assets_cover_asset_id_assets',
        'assets',
        'assets',
        ['cover_asset_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_index(
        op.f('ix_assets_cover_asset_id'), 'assets', ['cover_asset_id']
    )
    op.drop_constraint('ck_assets_kind_fields', 'assets', type_='check')
    op.create_check_constraint('ck_assets_kind_fields', 'assets', KIND_FIELDS)
    op.create_check_constraint('ck_assets_cover_link', 'assets', COVER_LINK)


def downgrade() -> None:
    covers = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM assets WHERE kind = 'cover'"))
        .scalar_one()
    )
    if covers:
        raise RuntimeError(
            f'{covers} cover asset(s) exist and cannot be kept without cover '
            'support; remove the covers of the audio tracks first'
        )
    op.drop_constraint('ck_assets_cover_link', 'assets', type_='check')
    op.drop_constraint('ck_assets_kind_fields', 'assets', type_='check')
    op.create_check_constraint('ck_assets_kind_fields', 'assets', OLD_KIND_FIELDS)
    op.drop_index(op.f('ix_assets_cover_asset_id'), table_name='assets')
    op.drop_constraint('fk_assets_cover_asset_id_assets', 'assets', type_='foreignkey')
    op.drop_column('assets', 'cover_asset_id')
