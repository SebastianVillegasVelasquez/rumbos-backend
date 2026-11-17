"""passback outbox and completion audit

A signed result per closed attempt waits in the outbox until Moodle has it; the
unique index on `attempt_id` makes enqueuing idempotent. `completion_audit` is
an append-only trail of what was sent and answered.

Revision ID: c7f1b3a8d204
Revises: b4e7a2c9d815
Create Date: 2026-11-17 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c7f1b3a8d204'
down_revision: Union[str, Sequence[str], None] = 'b4e7a2c9d815'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base() -> list[sa.Column]:
    return [
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        'passback_outbox',
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('moodle_course_id', sa.Integer(), nullable=False),
        sa.Column('attempt_id', sa.Uuid(), nullable=False),
        sa.Column('statement', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_try_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        *_base(),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['attempt_id'], ['assessment_attempts.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_passback_outbox_tenant_id'), 'passback_outbox', ['tenant_id']
    )
    op.create_index(
        'uq_passback_outbox_attempt', 'passback_outbox', ['attempt_id'], unique=True
    )
    op.create_index(
        'ix_passback_outbox_due',
        'passback_outbox',
        ['next_try_at'],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        'completion_audit',
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('moodle_course_id', sa.Integer(), nullable=False),
        sa.Column('attempt_id', sa.Uuid(), nullable=False),
        sa.Column('event', sa.String(length=24), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_completion_audit_tenant_id'), 'completion_audit', ['tenant_id']
    )
    op.create_index(
        op.f('ix_completion_audit_attempt_id'), 'completion_audit', ['attempt_id']
    )


def downgrade() -> None:
    op.drop_table('completion_audit')
    op.drop_table('passback_outbox')
