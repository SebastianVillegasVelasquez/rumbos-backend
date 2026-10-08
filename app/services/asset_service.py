import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from uuid_utils.compat import uuid7

from app.assets.processing import ImageLimits, process_image
from app.assets.storage import (
    AssetFileMissingError,
    AssetStorage,
    content_key,
    thumb_key,
)
from app.enums import AssetKind
from app.exceptions import (
    AssetAlreadyExistsError,
    AssetInvalidImageError,
    AssetNotFoundError,
    AssetQuotaExceededError,
    AssetTooLargeError,
    UploadsDisabledError,
)
from app.repositories.protocols import AssetRepository
from app.schemas.asset import AssetRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AssetSettings:
    """The upload policy, copied out of `Settings` so the service stays plain."""

    uploads_enabled: bool
    max_total_bytes: int
    max_background_bytes: int
    max_bubble_bytes: int
    max_background_side: int
    max_bubble_side: int

    def max_bytes_for(self, kind: AssetKind) -> int:
        if kind is AssetKind.BACKGROUND:
            return self.max_background_bytes
        return self.max_bubble_bytes

    @property
    def largest_upload_bytes(self) -> int:
        return max(self.max_background_bytes, self.max_bubble_bytes)


@dataclass(frozen=True, slots=True)
class UploadResult:
    asset: AssetRecord
    created: bool  # False: identical content of this kind was already there


@dataclass(frozen=True, slots=True)
class AssetContent:
    asset: AssetRecord
    data: bytes
    mime: str
    etag: str


class AssetService:
    """Validates, stores and serves uploaded images.

    SECURITY: there is no authentication yet, so uploads are anonymous. The
    quota and `uploads_enabled` only limit the damage; they are not protection.
    Uploads must stay disabled on any publicly reachable deployment until auth
    exists. Orphans (assets no map or skin uses) are never deleted for now;
    the quota bounds how many can pile up.
    """

    def __init__(
        self, assets: AssetRepository, storage: AssetStorage, settings: AssetSettings
    ) -> None:
        self._assets = assets
        self._storage = storage
        self._settings = settings

    def ensure_uploads_enabled(self) -> None:
        if not self._settings.uploads_enabled:
            raise UploadsDisabledError

    async def upload(self, file: BinaryIO, size: int, kind: AssetKind) -> UploadResult:
        """Checks the upload, re-encodes it and stores it (or finds the twin).

        `file` is the already-received upload, `size` its length in bytes.
        """
        self.ensure_uploads_enabled()
        limit = self._settings.max_bytes_for(kind)
        if size > limit:
            raise AssetTooLargeError(limit)
        if size == 0:
            raise AssetInvalidImageError("the file is empty")
        used = await self._assets.total_bytes()
        if used >= self._settings.max_total_bytes:
            raise AssetQuotaExceededError  # skip the decode when already full

        processed = await asyncio.to_thread(
            process_image,
            file,
            kind,
            ImageLimits(
                self._settings.max_background_side, self._settings.max_bubble_side
            ),
        )
        sha256 = hashlib.sha256(processed.data).hexdigest()
        existing = await self._assets.find_by_hash(kind, sha256)
        if existing is not None:
            return UploadResult(existing, created=False)
        # The quota is checked against the stored size, after re-encoding. Two
        # simultaneous uploads can overshoot it slightly; it is a damage
        # limiter, not an accounting system.
        if used + len(processed.data) > self._settings.max_total_bytes:
            raise AssetQuotaExceededError

        asset_id: uuid.UUID = uuid7()
        keys = [content_key(asset_id)]
        await self._storage.put(keys[0], processed.data)
        try:
            if processed.thumbnail is not None:
                keys.append(thumb_key(asset_id))
                await self._storage.put(keys[1], processed.thumbnail)
            record = await self._assets.create(
                asset_id,
                kind,
                processed.mime,
                processed.width,
                processed.height,
                len(processed.data),
                sha256,
            )
        except AssetAlreadyExistsError:
            # Lost a race against an identical upload: use the winner's.
            await self._discard(keys)
            winner = await self._assets.find_by_hash(kind, sha256)
            if winner is None:
                raise
            return UploadResult(winner, created=False)
        except BaseException:
            await self._discard(keys)
            raise
        return UploadResult(record, created=True)

    async def get_content(self, raw_id: str, *, thumb: bool = False) -> AssetContent:
        """The bytes to serve. Anything that is not a known asset id is not found.

        `raw_id` comes straight from the URL: it must be a canonical UUID, so
        nothing resembling a path ever reaches the storage layer.
        """
        asset_id = _parse_asset_id(raw_id)
        asset = await self._assets.get_by_id(asset_id)
        if asset is None or (thumb and asset.kind is not AssetKind.BACKGROUND):
            raise AssetNotFoundError(raw_id)
        key = thumb_key(asset_id) if thumb else content_key(asset_id)
        try:
            data = await self._storage.get(key)
        except AssetFileMissingError as exc:
            logger.error(
                "Asset %s is in the database but its file is missing", asset_id
            )
            raise AssetNotFoundError(raw_id) from exc
        if thumb:
            return AssetContent(asset, data, "image/webp", f'"{asset.sha256}-thumb"')
        return AssetContent(asset, data, asset.mime, f'"{asset.sha256}"')

    async def _discard(self, keys: list[str]) -> None:
        for key in keys:
            try:
                await self._storage.delete(key)
            except Exception:  # noqa: BLE001  best effort; never mask the real error
                logger.warning("Could not remove %s after a failed upload", key)


def _parse_asset_id(raw_id: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(raw_id)
    except ValueError as exc:
        raise AssetNotFoundError(raw_id) from exc
    if str(parsed) != raw_id.lower():  # braces, urn:, missing hyphens...
        raise AssetNotFoundError(raw_id)
    return parsed
