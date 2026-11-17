"""practice sessions, answers, saved words and reports

The practice trainer. A session is one sitting of one activity; an answer
carries a client-chosen `attempt_id` so a retried request is never counted
twice. Saved words live in Leitner boxes 1 to 5.

Revision ID: d2a9e5b7c316
Revises: c7f1b3a8d204
Create Date: 2026-11-17 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd2a9e5b7c316'
down_revision: Union[str, Sequence[str], None] = 'c7f1b3a8d204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base() -> list[sa.Column]:
    return [
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    ]


def _owner() -> list[sa.Column]:
    return [
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('moodle_course_id', sa.Integer(), nullable=False),
        sa.Column('sub', sa.String(length=128), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        'practice_sessions',
        *_owner(),
        sa.Column('activity_id', sa.String(length=80), nullable=False),
        sa.Column('content_version', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('seed', sa.String(length=32), nullable=False),
        sa.Column('state', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_base(),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_practice_sessions_tenant_id'), 'practice_sessions', ['tenant_id']
    )
    op.create_index(
        'uq_practice_sessions_one_open',
        'practice_sessions',
        ['tenant_id', 'moodle_course_id', 'sub', 'activity_id'],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
    )
    op.create_table(
        'practice_answers',
        sa.Column('session_id', sa.Uuid(), nullable=False),
        sa.Column('step_id', sa.String(length=80), nullable=False),
        sa.Column('attempt_id', sa.Uuid(), nullable=False),
        sa.Column('outcome', sa.String(length=20), nullable=False),
        sa.Column('earned', sa.Integer(), nullable=False),
        sa.Column('total', sa.Integer(), nullable=False),
        sa.Column('error_tag', sa.String(length=60), nullable=True),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(
            ['session_id'], ['practice_sessions.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'session_id', 'attempt_id', name='uq_practice_answers_session_attempt'
        ),
    )
    op.create_index(
        op.f('ix_practice_answers_session_id'), 'practice_answers', ['session_id']
    )
    op.create_table(
        'saved_words',
        *_owner(),
        sa.Column('entry_id', sa.String(length=80), nullable=False),
        sa.Column('box', sa.Integer(), nullable=False),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reviews', sa.Integer(), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant_id', 'moodle_course_id', 'sub', 'entry_id',
            name='uq_saved_words_user_entry',
        ),
    )
    op.create_index(op.f('ix_saved_words_tenant_id'), 'saved_words', ['tenant_id'])
    op.create_table(
        'practice_reports',
        *_owner(),
        sa.Column('activity_id', sa.String(length=80), nullable=False),
        sa.Column('step_id', sa.String(length=80), nullable=True),
        sa.Column('content_version', sa.String(length=32), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        *_base(),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_practice_reports_tenant_id'), 'practice_reports', ['tenant_id']
    )


def downgrade() -> None:
    op.drop_table('practice_reports')
    op.drop_table('saved_words')
    op.drop_table('practice_answers')
    op.drop_table('practice_sessions')
