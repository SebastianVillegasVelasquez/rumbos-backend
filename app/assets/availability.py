"""Whether the files an asset row promises are really in the storage.

Read live, never stored: it is not part of an asset, of a snapshot or of any
hash, only of the answer a client gets now. It costs one `stat` per file (no
content is read), so nothing is cached: a cache would only delay the moment a
restored volume makes assets playable again.
"""

import uuid
from collections.abc import Iterable

from app.assets.storage import AssetStorage, keys_of
from app.schemas.asset import AssetRecord


async def availability(
    storage: AssetStorage, records: Iterable[AssetRecord]
) -> dict[uuid.UUID, bool]:
    """For each asset, whether every file it keeps (original and thumbnail) is
    in the storage."""
    wanted = {r.id: keys_of(r.id, has_thumbnail=r.kind.has_thumbnail) for r in records}
    present = await storage.present([key for keys in wanted.values() for key in keys])
    return {
        asset_id: all(key in present for key in keys)
        for asset_id, keys in wanted.items()
    }
