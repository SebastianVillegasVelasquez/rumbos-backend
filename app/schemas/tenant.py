import uuid
from datetime import datetime

from app.enums import KeyStatus, TenantStatus
from app.schemas.base import ApiModel


class TenantRecord(ApiModel):
    """The `tenants` row, as repositories return it."""

    id: uuid.UUID
    name: str
    issuer: str
    host_origin: str
    status: TenantStatus
    asset_quota_bytes: int
    created_at: datetime


class TenantKeyRecord(ApiModel):
    """A `tenant_keys` row."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    kid: str
    public_key_pem: str
    status: KeyStatus
    created_at: datetime
    revoked_at: datetime | None


class NewTenantKey(ApiModel):
    """A key being registered; `public_key_pem` was already validated."""

    kid: str
    public_key_pem: str
