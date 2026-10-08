import uuid
from datetime import datetime

from app.enums import AssetKind
from app.schemas.base import ApiModel


class AssetRecord(ApiModel):
    """The `assets` row: what repositories return. Carries the hash (ETag)."""

    id: uuid.UUID
    kind: AssetKind
    mime: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    created_at: datetime


class AssetRead(ApiModel):
    """An asset as the API shows it. URLs are relative to the API root."""

    id: uuid.UUID
    kind: AssetKind
    mime: str
    width: int
    height: int
    bytes: int
    url: str
    thumb_url: str | None  # backgrounds only
    created_at: datetime

    @classmethod
    def from_record(cls, record: AssetRecord) -> "AssetRead":
        return cls(
            id=record.id,
            kind=record.kind,
            mime=record.mime,
            width=record.width,
            height=record.height,
            bytes=record.size_bytes,
            url=f"/assets/{record.id}",
            thumb_url=(
                f"/assets/{record.id}/thumb"
                if record.kind is AssetKind.BACKGROUND
                else None
            ),
            created_at=record.created_at,
        )
