import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import StringConstraints, field_validator, model_validator

from app.core.paths import ASSETS_PATH
from app.enums import AssetKind
from app.schemas.base import ApiModel

TITLE_MAX_LENGTH = 120
CREDIT_MAX_LENGTH = 300
LICENSE_MAX_LENGTH = 120
FILENAME_MAX_LENGTH = 255

AssetTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_LENGTH),
]
AssetCredit = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=CREDIT_MAX_LENGTH)
]


class AssetRecord(ApiModel):
    """The `assets` row: what repositories return. Carries the hash (ETag)."""

    id: uuid.UUID
    kind: AssetKind
    mime: str
    width: int | None  # none for audio
    height: int | None
    size_bytes: int
    sha256: str
    title: str
    credit: str | None
    original_filename: str | None
    created_at: datetime
    # None: a library track, shared by every tenant.
    tenant_id: uuid.UUID | None = None
    duration_seconds: float | None = None  # audio only
    license: str | None = None
    uploaded_by_sub: str | None = None
    rights_declared_at: datetime | None = None
    quarantined_at: datetime | None = None
    quarantine_reason: str | None = None

    @property
    def is_library(self) -> bool:
        return self.tenant_id is None

    @property
    def is_quarantined(self) -> bool:
        return self.quarantined_at is not None


class AssetAuditEntry(ApiModel):
    """One line of an asset's audit trail."""

    action: str  # created, quarantined or released
    reason: str | None
    created_at: datetime


class AssetCreate(ApiModel):
    """What a repository needs to store a new asset."""

    id: uuid.UUID
    kind: AssetKind
    mime: str
    width: int | None = None
    height: int | None = None
    size_bytes: int
    sha256: str
    title: str
    credit: str | None = None
    original_filename: str | None = None
    duration_seconds: float | None = None
    license: str | None = None
    # The rights trail of an audio upload.
    uploaded_by_sub: str | None = None
    rights_declared_at: datetime | None = None


class AssetRead(ApiModel):
    """An asset as the API shows it. URLs are relative to the API root."""

    id: uuid.UUID
    kind: AssetKind
    mime: str
    width: int | None  # images only
    height: int | None
    duration_seconds: float | None  # audio only
    bytes: int
    url: str
    thumb_url: str | None  # backgrounds only
    title: str
    credit: str | None
    license: str | None
    original_filename: str | None
    created_at: datetime

    @classmethod
    def from_record(cls, record: AssetRecord) -> "AssetRead":
        return cls(
            id=record.id,
            kind=record.kind,
            mime=record.mime,
            width=record.width,
            height=record.height,
            duration_seconds=record.duration_seconds,
            bytes=record.size_bytes,
            url=f"{ASSETS_PATH}/{record.id}",
            thumb_url=(
                f"{ASSETS_PATH}/{record.id}/thumb"
                if record.kind is AssetKind.BACKGROUND
                else None
            ),
            title=record.title,
            credit=record.credit,
            license=record.license,
            original_filename=record.original_filename,
            created_at=record.created_at,
        )


class AssetUsage(ApiModel):
    """How many things reference an asset (an asset in use cannot be deleted)."""

    maps: int = 0  # draft maps whose background it is
    skins: int = 0  # skins that mention it
    publications: int = 0  # publications (snapshots) that list it

    @property
    def total(self) -> int:
        return self.maps + self.skins + self.publications


class AssetListItem(AssetRead):
    usage: AssetUsage

    @classmethod
    def from_record_and_usage(
        cls, record: AssetRecord, usage: AssetUsage
    ) -> "AssetListItem":
        return cls(**AssetRead.from_record(record).model_dump(), usage=usage)


class AssetList(ApiModel):
    items: list[AssetListItem]
    total: int  # matches of the filters, ignoring limit/offset
    limit: int
    offset: int


class AssetUpdate(ApiModel):
    """Partial update: only fields present are applied. `credit: null` clears it."""

    title: AssetTitle | None = None
    credit: AssetCredit | None = None

    @field_validator("credit")
    @classmethod
    def _empty_credit_is_none(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        sent = self.model_fields_set
        if not sent:
            raise ValueError("at least one field must be provided")
        if "title" in sent and self.title is None:
            raise ValueError("title cannot be null")
        return self
