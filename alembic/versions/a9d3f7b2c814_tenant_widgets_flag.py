"""tenant widgets flag

Widgets (author-written HTML, CSS and JavaScript shown in a sandboxed iframe by
the frontend) are off for every tenant until an operator turns them on with
`python -m app.scripts.tenants set --issuer <iss> --widgets on`. Existing
tenants get the flag switched off.

Revision ID: a9d3f7b2c814
Revises: c4f8a2d6e913
Create Date: 2026-10-22 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9d3f7b2c814'
down_revision: Union[str, Sequence[str], None] = 'c4f8a2d6e913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tenants',
        sa.Column(
            'widgets_enabled',
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('tenants', 'widgets_enabled')
