"""Where a stored asset can be named: a map's `imageUrl` and a skin's config.

Pure functions, shared by validation (map create/update), the snapshot builder
and anything else that has to ask "which assets does this point at?".
"""

import re
import uuid
from collections.abc import Mapping
from typing import Any

from app.core.paths import ASSETS_PATH
from app.enums import AssetKind
from app.schemas.asset import AssetRecord
from app.schemas.map_settings import AudioInfo

# A map background served by this API: exactly `{ASSETS_PATH}/{canonical uuid}`.
_ASSET_PATH = re.compile(
    re.escape(ASSETS_PATH)
    + r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def asset_id_of_image_url(image_url: str) -> uuid.UUID | None:
    """The asset id when `image_url` is `{ASSETS_PATH}/{uuid}`, else None (any other
    URL or path is the editor's business). An upper-case id is read as the
    asset it names; use `is_canonical_asset_url` to tell the spellings apart."""
    match = _ASSET_PATH.fullmatch(image_url)
    return uuid.UUID(match[1]) if match else None


def is_canonical_asset_url(image_url: str) -> bool:
    """The asset URL spelled exactly as the API writes it (lower-case id)."""
    asset_id = asset_id_of_image_url(image_url)
    return asset_id is not None and image_url == f"{ASSETS_PATH}/{asset_id}"


def stored_asset_id(image_url: str) -> uuid.UUID | None:
    """The asset a stored `imageUrl` references: its id if canonical, else None."""
    return (
        asset_id_of_image_url(image_url) if is_canonical_asset_url(image_url) else None
    )


def audio_info_of(asset: AssetRecord | None) -> AudioInfo | None:
    """What a player shows about a map's music, or None when the asset cannot
    serve as music: it is gone, it is not audio, or it has been taken down."""
    if (
        asset is None
        or asset.kind is not AssetKind.AUDIO
        or asset.is_quarantined
        or asset.credit is None
        or asset.duration_seconds is None
    ):
        return None
    return AudioInfo(
        title=asset.title,
        credit=asset.credit,
        duration_seconds=asset.duration_seconds,
    )


def skin_asset_ids(config: Mapping[str, Any]) -> set[uuid.UUID]:
    """Every asset an image skin's state images point at (none for others)."""
    states = config.get("states")
    if not isinstance(states, Mapping):
        return set()
    found: set[uuid.UUID] = set()
    for value in states.values():
        if isinstance(value, str):
            try:
                found.add(uuid.UUID(value))
            except ValueError:
                continue
    return found
