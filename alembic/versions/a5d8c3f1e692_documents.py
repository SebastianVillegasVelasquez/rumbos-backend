"""documents

Assets can now be PDF documents for the steps of a welcome flow. A document
has no dimensions and no duration but a page count, always belongs to a tenant
and is stored exactly as uploaded. Its title may be up to 200 characters (the
column is widened from 120; every other kind is still held to 120 by the
service).

Nothing existing changes: every current row keeps satisfying the new checks.
Downgrading is refused while documents exist: there is nowhere to put them.

Revision ID: a5d8c3f1e692
Revises: e8c4b1a6d392
Create Date: 2026-11-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a5d8c3f1e692'
down_revision: Union[str, Sequence[str], None] = 'e8c4b1a6d392'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors app.models.asset; copied so the migration never changes with the app.
KIND_FIELDS = (
    "(kind = 'audio' AND width IS NULL AND height IS NULL"
    " AND duration_seconds > 0 AND credit IS NOT NULL)"
    " OR (kind = 'cover' AND width IS NOT NULL AND height IS NOT NULL"
    " AND duration_seconds IS NULL)"
    " OR (kind = 'document' AND width IS NULL AND height IS NULL"
    " AND duration_seconds IS NULL AND tenant_id IS NOT NULL)"
    " OR (kind NOT IN ('audio', 'cover', 'document') AND width IS NOT NULL"
    " AND height IS NOT NULL AND duration_seconds IS NULL"
    " AND tenant_id IS NOT NULL)"
)
PAGE_COUNT = (
    "(kind = 'document' AND page_count IS NOT NULL AND page_count > 0)"
    " OR (kind <> 'document' AND page_count IS NULL)"
)
# The check as it was before documents.
OLD_KIND_FIELDS = (
    "(kind = 'audio' AND width IS NULL AND height IS NULL"
    " AND duration_seconds > 0 AND credit IS NOT NULL)"
    " OR (kind = 'cover' AND width IS NOT NULL AND height IS NOT NULL"
    " AND duration_seconds IS NULL)"
    " OR (kind NOT IN ('audio', 'cover') AND width IS NOT NULL"
    " AND height IS NOT NULL AND duration_seconds IS NULL"
    " AND tenant_id IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column('assets', sa.Column('page_count', sa.Integer(), nullable=True))
    op.alter_column(
        'assets',
        'title',
        existing_type=sa.String(length=120),
        type_=sa.String(length=200),
        existing_nullable=False,
    )
    op.drop_constraint('ck_assets_kind_fields', 'assets', type_='check')
    op.create_check_constraint('ck_assets_kind_fields', 'assets', KIND_FIELDS)
    op.create_check_constraint('ck_assets_page_count', 'assets', PAGE_COUNT)


def downgrade() -> None:
    documents = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM assets WHERE kind = 'document'"))
        .scalar_one()
    )
    if documents:
        raise RuntimeError(
            f'{documents} document asset(s) exist and cannot be kept without '
            'document support; remove the PDF steps that use them and delete '
            'the documents first'
        )
    op.drop_constraint('ck_assets_page_count', 'assets', type_='check')
    op.drop_constraint('ck_assets_kind_fields', 'assets', type_='check')
    op.create_check_constraint('ck_assets_kind_fields', 'assets', OLD_KIND_FIELDS)
    op.alter_column(
        'assets',
        'title',
        existing_type=sa.String(length=200),
        type_=sa.String(length=120),
        existing_nullable=False,
    )
    op.drop_column('assets', 'page_count')
