"""exam mirror export: per-tenant switch and the definition outbox

Two nullable/defaulted columns on `tenants` (the plugin's mirror endpoint, off
when null, and whether student free text may be sent, off by default) and one
new table, `mirror_definitions`: the exam definitions waiting to be told to the
plugin, one per (tenant, form, definition hash). Additive: nothing changes for a
tenant until an operator sets its endpoint.

Revision ID: c7a3f9e1d542
Revises: e1f4a7c2b630
Create Date: 2026-11-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c7a3f9e1d542'
down_revision: Union[str, Sequence[str], None] = 'e1f4a7c2b630'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tenants', sa.Column('mirror_endpoint', sa.String(length=500), nullable=True)
    )
    op.add_column(
        'tenants',
        sa.Column(
            'mirror_written_text',
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.create_table(
        'mirror_definitions',
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('form_id', sa.String(length=80), nullable=False),
        sa.Column('definition_hash', sa.String(length=64), nullable=False),
        sa.Column('content_version', sa.String(length=32), nullable=False),
        sa.Column('statement', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_try_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_mirror_definitions_tenant_id'), 'mirror_definitions', ['tenant_id']
    )
    op.create_index(
        'uq_mirror_definitions_tenant_form_hash',
        'mirror_definitions',
        ['tenant_id', 'form_id', 'definition_hash'],
        unique=True,
    )
    op.create_index(
        'ix_mirror_definitions_due',
        'mirror_definitions',
        ['next_try_at'],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table('mirror_definitions')
    op.drop_column('tenants', 'mirror_written_text')
    op.drop_column('tenants', 'mirror_endpoint')
