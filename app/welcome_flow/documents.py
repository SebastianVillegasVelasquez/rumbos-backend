"""The documents a welcome flow shows, as they are now.

Read at request time from the asset rows and the storage: nothing here enters a
snapshot or its hash, so taking a document down or losing its file shows at once
without republishing, and a restore never has to rewrite history."""

import uuid
from collections.abc import Iterable

from app.assets.availability import availability
from app.assets.storage import AssetStorage
from app.core.paths import ASSETS_PATH
from app.enums import AssetKind
from app.repositories.protocols import AssetRepository
from app.schemas.welcome import WelcomeDocument


def unique_in_order(ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    return list(dict.fromkeys(ids))


async def usable_documents(
    assets: AssetRepository, storage: AssetStorage | None, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, WelcomeDocument]:
    """Every id with what is known of it. A document is `available` when this
    tenant owns it, it is of the document kind, it is not quarantined and (when
    a storage is given) its file is there."""
    wanted = unique_in_order(ids)
    found = await assets.get_many(wanted)
    live = [
        r
        for r in found.values()
        if r.kind is AssetKind.DOCUMENT and not r.is_quarantined
    ]
    present = await availability(storage, live) if storage is not None else {}
    result: dict[uuid.UUID, WelcomeDocument] = {}
    for asset_id in wanted:
        record = found.get(asset_id)
        url = f"{ASSETS_PATH}/{asset_id}"
        if (
            record is None
            or record.kind is not AssetKind.DOCUMENT
            or record.is_quarantined
            or not present.get(asset_id, storage is None)
        ):
            result[asset_id] = WelcomeDocument(
                asset_id=asset_id,
                url=url,
                title="",
                page_count=0,
                bytes=0,
                available=False,
            )
            continue
        result[asset_id] = WelcomeDocument(
            asset_id=asset_id,
            url=url,
            title=record.title,
            page_count=record.page_count or 0,
            bytes=record.size_bytes,
            available=True,
        )
    return result
