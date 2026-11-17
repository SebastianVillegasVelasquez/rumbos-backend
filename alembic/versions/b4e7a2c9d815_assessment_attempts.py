"""assessment attempts and answers

The native check-in and final exam. An attempt freezes its policy and, once
closed, its result; answers are one row per item, last write wins. At most one
attempt per (user, purpose) is in progress, and attempt numbers are dense, so
two tabs cannot consume two attempts.

Revision ID: b4e7a2c9d815
Revises: a5d8c3f1e692
Create Date: 2026-11-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b4e7a2c9d815'
down_revision: Union[str, Sequence[str], None] = 'a5d8c3f1e692'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'assessment_attempts',
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('moodle_course_id', sa.Integer(), nullable=False),
        sa.Column('sub', sa.String(length=128), nullable=False),
        sa.Column('purpose', sa.String(length=20), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('form_id', sa.String(length=80), nullable=False),
        sa.Column('content_version', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('preview', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('seed', sa.String(length=32), nullable=False),
        sa.Column('policy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant_id', 'moodle_course_id', 'sub', 'purpose', 'preview', 'ordinal',
            name='uq_assessment_attempts_user_purpose_ordinal',
        ),
    )
    op.create_index(
        op.f('ix_assessment_attempts_tenant_id'),
        'assessment_attempts',
        ['tenant_id'],
        unique=False,
    )
    op.create_index(
        'uq_assessment_attempts_one_open',
        'assessment_attempts',
        ['tenant_id', 'moodle_course_id', 'sub', 'purpose', 'preview'],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
    )
    op.create_table(
        'assessment_answers',
        sa.Column('attempt_id', sa.Uuid(), nullable=False),
        sa.Column('item_id', sa.String(length=80), nullable=False),
        sa.Column('response', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('saved_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['attempt_id'], ['assessment_attempts.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'attempt_id', 'item_id', name='uq_assessment_answers_attempt_item'
        ),
    )
    op.create_index(
        op.f('ix_assessment_answers_attempt_id'),
        'assessment_answers',
        ['attempt_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_assessment_answers_attempt_id'), table_name='assessment_answers'
    )
    op.drop_table('assessment_answers')
    op.drop_index('uq_assessment_attempts_one_open', table_name='assessment_attempts')
    op.drop_index(
        op.f('ix_assessment_attempts_tenant_id'), table_name='assessment_attempts'
    )
    op.drop_table('assessment_attempts')
