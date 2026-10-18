"""auth

Bearer tokens (stored as SHA-256 hashes) and the `jti`s of the launch
assertions already redeemed (a second use is a replay).

Revision ID: e5b2c8f4a136
Revises: d4a1b7e3c829
Create Date: 2026-10-18 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5b2c8f4a136'
down_revision: Union[str, Sequence[str], None] = 'd4a1b7e3c829'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'access_tokens',
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column(
            'role',
            sa.Enum(
                'editor', 'student', name='role', native_enum=False, length=20
            ),
            nullable=False,
        ),
        sa.Column('sub', sa.String(length=32), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('token_hash'),
    )
    op.create_index(
        op.f('ix_access_tokens_tenant_id'), 'access_tokens', ['tenant_id']
    )
    op.create_index('ix_access_tokens_expires_at', 'access_tokens', ['expires_at'])
    op.create_table(
        'used_jtis',
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('jti', sa.String(length=128), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id', 'jti'),
    )
    op.create_index('ix_used_jtis_expires_at', 'used_jtis', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_used_jtis_expires_at', table_name='used_jtis')
    op.drop_table('used_jtis')
    op.drop_index('ix_access_tokens_expires_at', table_name='access_tokens')
    op.drop_index(op.f('ix_access_tokens_tenant_id'), table_name='access_tokens')
    op.drop_table('access_tokens')
