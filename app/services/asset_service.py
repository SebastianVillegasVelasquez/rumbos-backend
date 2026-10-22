import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

from uuid_utils.compat import uuid7

from app.assets.audio import process_audio
from app.assets.metadata import clean_credit, clean_filename, default_title, fits_credit
from app.assets.processing import ImageLimits, process_image
from app.assets.storage import (
    AssetFileMissingError,
    AssetStorage,
    content_key,
    thumb_key,
)
from app.core.config import Settings
from app.enums import AssetKind, AssetScope
from app.exceptions import (
    AssetAlreadyExistsError,
    AssetInvalidImageError,
    AssetIsLibraryError,
    AssetNotFoundError,
    AssetQuotaExceededError,
    AssetTooLargeError,
    AudioCreditRequiredError,
    AudioInvalidError,
    AudioRightsDeclarationRequiredError,
    AudioUploadsDisabledError,
    InvalidAssetMetadataError,
    UploadsDisabledError,
)
from app.repositories.protocols import AssetRepository
from app.schemas.asset import (
    CREDIT_MAX_LENGTH,
    LICENSE_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    AssetCreate,
    AssetList,
    AssetListItem,
    AssetRecord,
    AssetUpdate,
)

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
    audio_uploads_enabled: bool = True
    max_audio_bytes: int = 8 * 1024 * 1024
    max_audio_seconds: float = 600.0

    @classmethod
    def from_app_settings(cls, settings: Settings) -> "AssetSettings":
        return cls(
            uploads_enabled=settings.assets_uploads_enabled,
            max_total_bytes=settings.assets_max_total_bytes,
            max_background_bytes=settings.assets_max_background_bytes,
            max_bubble_bytes=settings.assets_max_bubble_bytes,
            max_background_side=settings.assets_max_background_side,
            max_bubble_side=settings.assets_max_bubble_side,
            audio_uploads_enabled=settings.audio_uploads_on,
            max_audio_bytes=settings.assets_max_audio_bytes,
            max_audio_seconds=settings.assets_max_audio_seconds,
        )

    def max_bytes_for(self, kind: AssetKind) -> int:
        if kind is AssetKind.AUDIO:
            return self.max_audio_bytes
        if kind is AssetKind.BACKGROUND:
            return self.max_background_bytes
        return self.max_bubble_bytes

    def enabled_for(self, kind: AssetKind | None) -> bool:
        """Whether uploads of `kind` are on; None: whether any are."""
        if kind is None:
            return self.uploads_enabled or self.audio_uploads_enabled
        if kind is AssetKind.AUDIO:
            return self.audio_uploads_enabled
        return self.uploads_enabled

    @property
    def largest_upload_bytes(self) -> int:
        """The biggest file any enabled kind accepts: how much body is read."""
        sizes = [
            self.max_bytes_for(kind) for kind in AssetKind if self.enabled_for(kind)
        ]
        return max(sizes, default=0)


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
    exists. Deleting is anonymous too (see `delete`): anyone who can reach the
    API can remove an UNUSED image. Assets in use are protected by the usage
    check, so a published map can never lose its picture.
    """

    def __init__(
        self, assets: AssetRepository, storage: AssetStorage, settings: AssetSettings
    ) -> None:
        self._assets = assets
        self._storage = storage
        self._settings = settings

    def ensure_uploads_enabled(self, kind: AssetKind | None = None) -> None:
        """Raises the 503 of the switch that governs `kind` (None: of both)."""
        if self._settings.enabled_for(kind):
            return
        if kind is AssetKind.AUDIO:
            raise AudioUploadsDisabledError
        raise UploadsDisabledError

    async def upload(
        self,
        file: BinaryIO,
        size: int,
        kind: AssetKind,
        *,
        title: str | None = None,
        credit: str | None = None,
        filename: str | None = None,
    ) -> UploadResult:
        """Checks the upload, re-encodes it and stores it (or finds the twin).

        `file` is the already-received upload, `size` its length in bytes.
        `title` defaults to the file name without its extension. An identical
        upload returns the existing asset untouched: its title and credit are
        NOT overwritten by the new ones.
        """
        self.ensure_uploads_enabled(kind)
        original_filename = clean_filename(filename)
        clean_title = (
            _clean_title(title)
            if title is not None and title.strip()
            else default_title(original_filename, kind)
        )
        clean_cred = clean_credit(credit)
        if not fits_credit(clean_cred):
            raise InvalidAssetMetadataError(f"credit over {CREDIT_MAX_LENGTH}")
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
                AssetCreate(
                    id=asset_id,
                    kind=kind,
                    mime=processed.mime,
                    width=processed.width,
                    height=processed.height,
                    size_bytes=len(processed.data),
                    sha256=sha256,
                    title=clean_title,
                    credit=clean_cred,
                    original_filename=original_filename,
                )
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

    async def upload_audio(
        self,
        file: BinaryIO,
        size: int,
        *,
        title: str | None,
        credit: str | None,
        license: str | None,
        rights_declared: bool,
        uploaded_by_sub: str | None,
    ) -> UploadResult:
        """An editor's own audio track, for their tenant's library.

        The upload must come with the declaration that its author has the
        rights to it, and with a credit. What is kept about the person is only
        their pseudonymous `uploaded_by_sub`, with the moment they declared.
        An identical track already there is returned untouched.
        """
        self.ensure_uploads_enabled(AssetKind.AUDIO)
        if not rights_declared:
            raise AudioRightsDeclarationRequiredError
        result = await self._store_audio(
            file,
            size,
            title=title,
            credit=credit,
            license=license,
            uploaded_by_sub=uploaded_by_sub,
            library=False,
        )
        if result.created:
            logger.info(
                "Audio %s uploaded by sub=%s, rights declared, license=%r",
                result.asset.id,
                uploaded_by_sub,
                result.asset.license,
            )
        return result

    async def import_library_audio(
        self,
        file: BinaryIO,
        size: int,
        *,
        title: str | None,
        credit: str | None,
        license: str | None,
    ) -> UploadResult:
        """A track for the shared library, checked exactly like an upload. For
        the operator's tools: it needs the system scope, and it neither depends
        on the upload switch nor counts against any quota."""
        return await self._store_audio(
            file,
            size,
            title=title,
            credit=credit,
            license=license,
            uploaded_by_sub=None,
            library=True,
        )

    async def _store_audio(
        self,
        file: BinaryIO,
        size: int,
        *,
        title: str | None,
        credit: str | None,
        license: str | None,
        uploaded_by_sub: str | None,
        library: bool,
    ) -> UploadResult:
        clean_title = _clean_title(title) if title and title.strip() else None
        if clean_title is None:
            raise InvalidAssetMetadataError("an audio track needs a title")
        clean_cred = clean_credit(credit)
        if clean_cred is None:
            raise AudioCreditRequiredError
        if not fits_credit(clean_cred):
            raise InvalidAssetMetadataError(f"credit over {CREDIT_MAX_LENGTH}")
        clean_license = clean_credit(license)
        if clean_license is not None and len(clean_license) > LICENSE_MAX_LENGTH:
            raise InvalidAssetMetadataError(f"license over {LICENSE_MAX_LENGTH}")
        limit = self._settings.max_audio_bytes
        if size > limit:
            raise AssetTooLargeError(limit)
        if size == 0:
            raise AudioInvalidError("the file is empty")
        used = await self._assets.total_bytes()
        if not library and used >= self._settings.max_total_bytes:
            raise AssetQuotaExceededError  # skip the parse when already full

        try:
            processed = await asyncio.to_thread(
                process_audio, file, self._settings.max_audio_seconds
            )
        except AudioInvalidError as exc:
            logger.info("Audio refused: %s", exc.reason)
            raise
        sha256 = hashlib.sha256(processed.data).hexdigest()
        existing = await self._assets.find_by_hash(
            AssetKind.AUDIO, sha256, library=library
        )
        if existing is not None:
            return UploadResult(existing, created=False)
        if not library and used + len(processed.data) > self._settings.max_total_bytes:
            raise AssetQuotaExceededError

        asset_id: uuid.UUID = uuid7()
        key = content_key(asset_id)
        await self._storage.put(key, processed.data)
        try:
            record = await self._assets.create(
                AssetCreate(
                    id=asset_id,
                    kind=AssetKind.AUDIO,
                    mime=processed.mime,
                    size_bytes=len(processed.data),
                    sha256=sha256,
                    title=clean_title,
                    credit=clean_cred,
                    duration_seconds=processed.duration_seconds,
                    license=clean_license,
                    uploaded_by_sub=uploaded_by_sub,
                    rights_declared_at=datetime.now(UTC),
                )
            )
        except AssetAlreadyExistsError:
            # Lost a race against an identical upload: use the winner's.
            await self._discard([key])
            winner = await self._assets.find_by_hash(
                AssetKind.AUDIO, sha256, library=library
            )
            if winner is None:
                raise
            return UploadResult(winner, created=False)
        except BaseException:
            await self._discard([key])
            raise
        return UploadResult(record, created=True)

    async def list_assets(
        self,
        *,
        kind: AssetKind | None,
        q: str | None,
        limit: int,
        offset: int,
        scope: AssetScope = AssetScope.TENANT,
    ) -> AssetList:
        records, total = await self._assets.list(
            kind, (q or "").strip() or None, limit, offset, scope
        )
        usage = await self._assets.usage([r.id for r in records])
        return AssetList(
            items=[
                AssetListItem.from_record_and_usage(r, usage[r.id]) for r in records
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def update_metadata(
        self, asset_id: uuid.UUID, data: AssetUpdate
    ) -> AssetRecord:
        asset = await self._require_own(asset_id)
        if (
            asset.kind is AssetKind.AUDIO
            and "credit" in data.model_fields_set
            and not data.credit
        ):
            raise AudioCreditRequiredError  # an audio track is never uncredited
        updated = await self._assets.update_metadata(asset_id, data)
        if updated is None:
            raise AssetNotFoundError(str(asset_id))
        return updated

    async def delete(self, asset_id: uuid.UUID) -> None:
        """Deletes an asset nothing references.

        Order: the database row first, in one statement that also checks that no
        map, skin or publication uses it (`AssetInUseError` otherwise); only
        then the files. The row is what makes an asset reachable, so if removing
        the files fails the worst case is unreachable bytes on disk (logged,
        and removal is idempotent), never an asset that is listed but has lost
        its picture. The usage check is not a lock: a map that starts using the
        image in the same instant as the delete can still lose it.
        """
        await self._require_own(asset_id)
        if not await self._assets.delete_if_unused(asset_id):
            raise AssetNotFoundError(str(asset_id))
        for key in (thumb_key(asset_id), content_key(asset_id)):
            try:
                await self._storage.delete(key)
            except Exception:  # noqa: BLE001  the asset is gone either way
                logger.error("Asset %s deleted but %s was not removed", asset_id, key)

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

    async def _require_own(self, asset_id: uuid.UUID) -> AssetRecord:
        """The asset, if the caller may change it: a library track is visible
        but not theirs (`AssetIsLibraryError`); anything else not visible does
        not exist."""
        asset = await self._assets.get_by_id(asset_id)
        if asset is None:
            raise AssetNotFoundError(str(asset_id))
        if asset.is_library:
            raise AssetIsLibraryError
        return asset

    async def _discard(self, keys: list[str]) -> None:
        for key in keys:
            try:
                await self._storage.delete(key)
            except Exception:  # noqa: BLE001  best effort; never mask the real error
                logger.warning("Could not remove %s after a failed upload", key)


def _clean_title(raw: str) -> str:
    cleaned = " ".join(raw.split())
    if not cleaned or len(cleaned) > TITLE_MAX_LENGTH:
        raise InvalidAssetMetadataError(f"title must be 1-{TITLE_MAX_LENGTH} chars")
    return cleaned


def _parse_asset_id(raw_id: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(raw_id)
    except ValueError as exc:
        raise AssetNotFoundError(raw_id) from exc
    if str(parsed) != raw_id.lower():  # braces, urn:, missing hyphens...
        raise AssetNotFoundError(raw_id)
    return parsed
