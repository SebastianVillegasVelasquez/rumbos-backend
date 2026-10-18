import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import KeyStatus, TenantStatus
from app.models.base import BaseORM

UQ_TENANT_KEY_KID = "uq_tenant_keys_tenant_kid"

# The tenant every pre-existing row belongs to, and the one the anonymous
# (`LEGACY_ANONYMOUS_MODE`) principal acts as. A fixed id so the migration, the
# tests and the code agree on it. It has no keys, so no launch can ever name it.
LEGACY_TENANT_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")
LEGACY_TENANT_ISSUER = "https://legacy.invalid"


def _enum_column(enum_cls: type[TenantStatus] | type[KeyStatus]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=20,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class Tenant(BaseORM):
    """A Moodle site registered to use Rumbos. Created only by the CLI."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(120))
    # The Moodle wwwroot (https, no trailing slash): the launch assertion's `iss`.
    issuer: Mapped[str] = mapped_column(String(255), unique=True)
    # Where the app is embedded from; feeds the `frame-ancestors` policy.
    host_origin: Mapped[str] = mapped_column(String(255))
    status: Mapped[TenantStatus] = mapped_column(
        _enum_column(TenantStatus), default=TenantStatus.ACTIVE
    )
    # What this tenant may store in total (`ASSETS_MAX_TOTAL_BYTES` is the default).
    asset_quota_bytes: Mapped[int] = mapped_column(BigInteger)


class TenantKey(BaseORM):
    """A public key a tenant's Moodle signs launch assertions with."""

    __tablename__ = "tenant_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "kid", name=UQ_TENANT_KEY_KID),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    kid: Mapped[str] = mapped_column(String(128))
    public_key_pem: Mapped[str] = mapped_column(Text)
    status: Mapped[KeyStatus] = mapped_column(
        _enum_column(KeyStatus), default=KeyStatus.ACTIVE
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
