"""content bubbles

A bubble can now be content written in Rumbos instead of a Moodle activity:
`kind` is 'activity' (every existing row) or 'content', and `content` holds the
validated blocks or widget of a content bubble. `activity_id` is nullable, and a
check keeps the two shapes apart: an activity bubble has an activity and no
content, a content bubble has content and no activity.

The rule "an activity appears once per course of a tenant" becomes a partial
unique index over activity bubbles only (same name, so errors keep mapping to
it). Existing rows keep working untouched.

Downgrading is refused while content bubbles exist: they have nowhere to go.

Revision ID: b6e1c8a4d273
Revises: a9d3f7b2c814
Create Date: 2026-10-22 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b6e1c8a4d273'
down_revision: Union[str, Sequence[str], None] = 'a9d3f7b2c814'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UNIQUE = 'uq_bubbles_moodle_course_id_activity_id'
CHECK = 'ck_bubbles_kind_fields'
# Mirrors app.models.bubble; copied so the migration never changes with the app.
KIND_FIELDS = (
    "(kind = 'activity' AND activity_id IS NOT NULL AND content IS NULL)"
    " OR (kind = 'content' AND activity_id IS NULL AND content IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column(
        'bubbles',
        sa.Column(
            'kind', sa.String(length=20), server_default='activity', nullable=False
        ),
    )
    op.add_column(
        'bubbles',
        sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column('bubbles', 'activity_id', nullable=True)
    op.create_check_constraint(CHECK, 'bubbles', KIND_FIELDS)
    op.drop_constraint(UNIQUE, 'bubbles', type_='unique')
    op.create_index(
        UNIQUE,
        'bubbles',
        ['tenant_id', 'moodle_course_id', 'activity_id'],
        unique=True,
        postgresql_where=sa.text("kind = 'activity'"),
    )


def downgrade() -> None:
    content = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM bubbles WHERE kind = 'content'"))
        .scalar_one()
    )
    if content:
        raise RuntimeError(
            f'{content} content bubble(s) exist and cannot be kept without '
            'content bubbles; delete them first'
        )
    op.drop_index(UNIQUE, table_name='bubbles')
    op.create_unique_constraint(
        UNIQUE, 'bubbles', ['tenant_id', 'moodle_course_id', 'activity_id']
    )
    op.drop_constraint(CHECK, 'bubbles', type_='check')
    op.alter_column('bubbles', 'activity_id', nullable=False)
    op.drop_column('bubbles', 'content')
    op.drop_column('bubbles', 'kind')
