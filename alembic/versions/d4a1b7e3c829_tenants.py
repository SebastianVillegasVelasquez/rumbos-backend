"""tenants

Each Moodle site that uses Rumbos is a tenant, with the public keys its
launch assertions are verified against. The "legacy" tenant (fixed id) stands
for the standalone deployment that exists today: the next migration attaches
every existing row to it.

Revision ID: d4a1b7e3c829
Revises: a7c4e1d90b35
Create Date: 2026-10-18 10:00:00.000000

"""

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4a1b7e3c829"
down_revision: Union[str, Sequence[str], None] = "a7c4e1d90b35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors app.models.tenant; copied so the migration never changes with the app.
LEGACY_TENANT_ID = "00000000-0000-7000-8000-000000000001"
LEGACY_ISSUER = "https://legacy.invalid"


def upgrade() -> None:
    tenants = op.create_table(
        "tenants",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("host_origin", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "disabled",
                name="tenantstatus",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("asset_quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer"),
    )
    op.create_table(
        "tenant_keys",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kid", sa.String(length=128), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "revoked",
                name="keystatus",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "kid", name="uq_tenant_keys_tenant_kid"),
    )
    op.create_index(op.f("ix_tenant_keys_tenant_id"), "tenant_keys", ["tenant_id"])

    from app.core.config import get_settings

    now = datetime.now(timezone.utc)
    op.bulk_insert(
        tenants,
        [
            {
                "id": uuid.UUID(LEGACY_TENANT_ID),
                "name": "legacy",
                "issuer": LEGACY_ISSUER,
                "host_origin": LEGACY_ISSUER,
                "status": "active",
                "asset_quota_bytes": get_settings().assets_max_total_bytes,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_keys_tenant_id"), table_name="tenant_keys")
    op.drop_table("tenant_keys")
    op.drop_table("tenants")
