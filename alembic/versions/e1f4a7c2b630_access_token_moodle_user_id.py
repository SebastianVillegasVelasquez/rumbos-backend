"""access tokens carry the Moodle user id

The plugin signs the Moodle user id (`uid`, an integer, optional) in the launch
assertion. It is kept only on the short-lived access token (expired rows are
purged), so the final-exam gate can ask Moodle about that user. Nullable and
additive: tokens issued before this keep working and read as "no user id".

Revision ID: e1f4a7c2b630
Revises: d2a9e5b7c316
Create Date: 2026-11-22 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1f4a7c2b630'
down_revision: Union[str, Sequence[str], None] = 'd2a9e5b7c316'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'access_tokens', sa.Column('moodle_user_id', sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('access_tokens', 'moodle_user_id')
