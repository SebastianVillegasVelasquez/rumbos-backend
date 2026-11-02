import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import AssetKind
from app.models.base import BaseORM

# The same bytes uploaded twice by a tenant as the same kind are one asset. The database
# enforces it, so two simultaneous identical uploads cannot both create a row.
UQ_ASSET_CONTENT = "uq_assets_kind_sha256"
# The same, for the library (no tenant): NULLs are distinct in a unique
# constraint, so a partial index is what keeps a library track from repeating.
UQ_LIBRARY_CONTENT = "uq_assets_library_kind_sha256"


class Asset(BaseORM):
    """Metadata of an uploaded image, audio track or cover. The bytes live in
    an `AssetStorage`.

    An audio track with no tenant belongs to the library: curated by the
    operator, readable and selectable by every tenant, changed by nobody else.
    Images always belong to a tenant; a cover belongs to whoever its track does."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "sha256", name=UQ_ASSET_CONTENT),
        Index(
            UQ_LIBRARY_CONTENT,
            "kind",
            "sha256",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
        ),
        CheckConstraint("width > 0 AND height > 0", name="ck_assets_dimensions"),
        CheckConstraint("bytes > 0", name="ck_assets_bytes"),
        # Images have dimensions and a tenant; audio has a duration and a credit
        # and may be global; a cover has dimensions and is global when its
        # track is (a library track's cover belongs to the library too).
        CheckConstraint(
            "(kind = 'audio' AND width IS NULL AND height IS NULL"
            " AND duration_seconds > 0 AND credit IS NOT NULL)"
            " OR (kind = 'cover' AND width IS NOT NULL AND height IS NOT NULL"
            " AND duration_seconds IS NULL)"
            " OR (kind NOT IN ('audio', 'cover') AND width IS NOT NULL"
            " AND height IS NOT NULL AND duration_seconds IS NULL"
            " AND tenant_id IS NOT NULL)",
            name="ck_assets_kind_fields",
        ),
        # Only an audio track has a cover, and never itself.
        CheckConstraint(
            "cover_asset_id IS NULL OR (kind = 'audio' AND cover_asset_id <> id)",
            name="ck_assets_cover_link",
        ),
    )

    # The tenant that uploaded it; its library, its quota. None: the shared
    # library (audio only).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id"), index=True
    )
    kind: Mapped[AssetKind] = mapped_column(
        Enum(
            AssetKind,
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
        )
    )
    mime: Mapped[str] = mapped_column(String(32))
    # Pixels; none for audio.
    width: Mapped[int | None]
    height: Mapped[int | None]
    # Seconds; audio only.
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    # Size of the stored (re-encoded) file; what the quota adds up.
    size_bytes: Mapped[int] = mapped_column("bytes", BigInteger)
    # Hex digest of the stored bytes. Also the HTTP ETag.
    sha256: Mapped[str] = mapped_column(String(64))
    # Library metadata, shown to editors. Never part of a file path.
    title: Mapped[str] = mapped_column(String(120))
    credit: Mapped[str | None] = mapped_column(String(300))
    # What the uploader's file was called. Display only; sanitized on the way in.
    original_filename: Mapped[str | None] = mapped_column(String(255))
    # Rights trail of an upload. `uploaded_by_sub` is the launch's pseudonymous
    # user id and the only thing about the person that is kept.
    license: Mapped[str | None] = mapped_column(String(120))
    uploaded_by_sub: Mapped[str | None] = mapped_column(String(32))
    rights_declared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The picture of an audio track: an asset of kind `cover`, shared by every
    # track that uses the same one. While a track points at it the cover cannot
    # be deleted (nor collected as garbage); deleting the track releases it.
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), index=True
    )
    # Takedown: while set, the asset is not served, listed or selectable.
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quarantine_reason: Mapped[str | None] = mapped_column(Text)


class AssetAuditEvent(BaseORM):
    """One line of the asset audit trail, kept even after the asset is deleted
    (so `asset_id` is not a foreign key)."""

    __tablename__ = "asset_audit_events"

    asset_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # created, quarantined or released.
    action: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
