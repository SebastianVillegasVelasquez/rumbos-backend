import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseORM

UQ_PUBLICATION_NUMBER = "uq_publications_course_map_number"
IX_PUBLICATION_ASSETS = "ix_publications_asset_ids"


class Publication(BaseORM):
    """An immutable snapshot of a map, what students see.

    The map's CURRENT publication is simply its highest `number`: there is no
    pointer to keep in sync. Rows are only ever inserted, and deleted when
    they age out (`PUBLICATIONS_KEEP`) or with their map.
    """

    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("course_map_id", "number", name=UQ_PUBLICATION_NUMBER),
        # "Which publications use this asset?" without opening every snapshot.
        Index(IX_PUBLICATION_ASSETS, "asset_ids", postgresql_using="gin"),
    )

    course_map_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_maps.id", ondelete="CASCADE")
    )
    # 1, 2, 3... per map. Numbers are never reused (pruning only removes old ones).
    number: Mapped[int]
    # Content as built by `app.publishing.snapshot` (schemaVersion 1).
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # SHA-256 of the snapshot's canonical JSON.
    content_hash: Mapped[str]
    # Every asset the snapshot depends on (its background and its skins' images).
    asset_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    note: Mapped[str | None]
