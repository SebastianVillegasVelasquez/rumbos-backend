from sqlalchemy import BigInteger, CheckConstraint, Enum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import AssetKind
from app.models.base import BaseORM

# The same bytes uploaded twice as the same kind are one asset. The database
# enforces it, so two simultaneous identical uploads cannot both create a row.
UQ_ASSET_CONTENT = "uq_assets_kind_sha256"


class Asset(BaseORM):
    """Metadata of an uploaded image. The bytes live in an `AssetStorage`."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("kind", "sha256", name=UQ_ASSET_CONTENT),
        CheckConstraint("width > 0 AND height > 0", name="ck_assets_dimensions"),
        CheckConstraint("bytes > 0", name="ck_assets_bytes"),
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
    width: Mapped[int]
    height: Mapped[int]
    # Size of the stored (re-encoded) file; what the quota adds up.
    size_bytes: Mapped[int] = mapped_column("bytes", BigInteger)
    # Hex digest of the stored bytes. Also the HTTP ETag.
    sha256: Mapped[str] = mapped_column(String(64))
