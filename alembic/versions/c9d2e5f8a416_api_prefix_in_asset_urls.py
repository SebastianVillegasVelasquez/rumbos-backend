"""api prefix in asset urls

The API is now served under /api/v1, and so are the images it serves. A map
whose background is an uploaded image stored `/assets/{id}`; it must now read
`/api/v1/assets/{id}`. Published snapshots hold the same string, so they are
rewritten too and their content hash is recomputed (otherwise every such map
would suddenly show "unpublished changes").

Revision ID: c9d2e5f8a416
Revises: b8e4c1a7d035
Create Date: 2026-10-12 09:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d2e5f8a416'
down_revision: Union[str, Sequence[str], None] = 'b8e4c1a7d035'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PREFIX = '/api/v1'
UUID_RE = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def _content_hash(snapshot: dict) -> str:
    # Same canonical hash the application computes; imported so the two can
    # never disagree about it.
    from app.publishing.snapshot import content_hash

    return content_hash(snapshot)


def _rewrite(old_start: str, new_start: str) -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE course_maps SET image_url = :new || substr(image_url, :cut)"
            " WHERE image_url ~ :pattern"
        ),
        {
            'new': new_start,
            'cut': len(old_start) + 1,
            'pattern': '^' + old_start + '/' + UUID_RE + '$',
        },
    )
    rows = conn.execute(
        sa.text(
            "SELECT id, snapshot FROM publications"
            " WHERE snapshot->>'imageUrl' ~ :pattern"
        ),
        {'pattern': '^' + old_start + '/' + UUID_RE + '$'},
    ).all()
    for row_id, snapshot in rows:
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        snapshot['imageUrl'] = new_start + snapshot['imageUrl'][len(old_start):]
        conn.execute(
            sa.text(
                "UPDATE publications SET snapshot = CAST(:s AS jsonb),"
                " content_hash = :h WHERE id = :i"
            ),
            {'s': json.dumps(snapshot), 'h': _content_hash(snapshot), 'i': row_id},
        )


def upgrade() -> None:
    _rewrite('/assets', PREFIX + '/assets')


def downgrade() -> None:
    _rewrite(PREFIX + '/assets', '/assets')
