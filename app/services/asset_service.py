import asyncio
import hashlib
import io
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

from uuid_utils.compat import uuid7

from app.assets.artwork import find_artwork
from app.assets.audio import process_audio
from app.assets.availability import availability
from app.assets.document import inspect_pdf
from app.assets.metadata import clean_credit, clean_filename, default_title, fits_credit
from app.assets.processing import (
    ImageLimits,
    ProcessedCover,
    process_cover,
    process_image,
)
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
    AssetInUseError,
    AssetInvalidImageError,
    AssetIsLibraryError,
    AssetNotFoundError,
    AssetQuotaExceededError,
    AssetTooLargeError,
    AudioCreditRequiredError,
    AudioInvalidError,
    AudioQuarantinedError,
    AudioRightsDeclarationRequiredError,
    AudioUploadsDisabledError,
    CoverInvalidError,
    CoverTooLargeError,
    DocumentInvalidError,
    DocumentQuarantinedError,
    DocumentRightsDeclarationRequiredError,
    InvalidAssetMetadataError,
    InvalidUploadError,
    UploadsDisabledError,
)
from app.repositories.protocols import AssetRepository
from app.schemas.asset import (
    CREDIT_MAX_LENGTH,
    DOCUMENT_TITLE_MAX_LENGTH,
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
    max_image_bytes: int = 4 * 1024 * 1024
    max_image_side: int = 2048
    max_cover_bytes: int = 2 * 1024 * 1024
    max_cover_side: int = 2048
    max_document_bytes: int = 10 * 1024 * 1024
    document_scan_timeout_seconds: float = 10.0

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
            max_image_bytes=settings.assets_max_image_bytes,
            max_image_side=settings.assets_max_image_side,
            max_cover_bytes=settings.assets_max_cover_bytes,
            max_cover_side=settings.assets_max_cover_side,
            max_document_bytes=settings.document_max_bytes,
            document_scan_timeout_seconds=settings.document_scan_timeout_seconds,
        )

    def max_bytes_for(self, kind: AssetKind) -> int:
        if kind is AssetKind.AUDIO:
            return self.max_audio_bytes
        if kind is AssetKind.COVER:
            return self.max_cover_bytes
        if kind is AssetKind.BACKGROUND:
            return self.max_background_bytes
        if kind is AssetKind.IMAGE:
            return self.max_image_bytes
        if kind is AssetKind.DOCUMENT:
            return self.max_document_bytes
        return self.max_bubble_bytes

    def enabled_for(self, kind: AssetKind | None) -> bool:
        """Whether uploads of `kind` are on; None: whether any are."""
        if kind is None:
            return self.uploads_enabled or self.audio_uploads_enabled
        if kind in (AssetKind.AUDIO, AssetKind.COVER):
            return self.audio_uploads_enabled  # a cover is part of a track
        return self.uploads_enabled

    @property
    def image_limits(self) -> ImageLimits:
        return ImageLimits(
            self.max_background_side,
            self.max_bubble_side,
            self.max_image_side,
            self.max_cover_side,
        )

    @property
    def largest_body_bytes(self) -> int:
        """How much body an upload may send: its biggest file, plus a cover
        when audio can be uploaded (a track and its cover travel together)."""
        extra = self.max_cover_bytes if self.audio_uploads_enabled else 0
        return self.largest_upload_bytes + extra

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
    # Things the upload got through but the editor should know: "cover_dropped"
    # (the picture found inside the audio could not be used).
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CoverUpload:
    """A cover the editor sent along with a track."""

    file: BinaryIO
    size: int


@dataclass(frozen=True, slots=True)
class AssetContent:
    asset: AssetRecord
    data: bytes
    mime: str
    etag: str


@dataclass(frozen=True, slots=True)
class _StoredCover:
    record: AssetRecord
    created: bool  # False: an identical cover was already there and is reused


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
        if kind in (AssetKind.AUDIO, AssetKind.COVER):
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
        if kind is AssetKind.DOCUMENT:
            raise InvalidUploadError("documents are uploaded with upload_document")
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
                self._settings.max_background_side,
                self._settings.max_bubble_side,
                self._settings.max_image_side,
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

    async def upload_document(
        self,
        file: BinaryIO,
        size: int,
        *,
        title: str | None,
        rights_declared: bool,
        uploaded_by_sub: str | None,
    ) -> UploadResult:
        """A PDF for the welcome flows of this tenant.

        It must come with a title and with the declaration that the uploader
        may share it. The file is inspected (`app.assets.document`) in a worker
        thread under a timeout and, if accepted, stored EXACTLY as uploaded:
        its metadata is not stripped. Only the title, the page count, the
        size and the pseudonymous `uploaded_by_sub` are kept about it; the
        name the client gave the file is dropped. An identical document
        already there is returned untouched."""
        self.ensure_uploads_enabled(AssetKind.DOCUMENT)
        if not rights_declared:
            raise DocumentRightsDeclarationRequiredError
        if title is None or not title.strip():
            raise InvalidAssetMetadataError("a document needs a title")
        clean_title = _clean_title(title, DOCUMENT_TITLE_MAX_LENGTH)
        limit = self._settings.max_document_bytes
        if size > limit:
            raise AssetTooLargeError(limit)
        if size == 0:
            raise DocumentInvalidError("not_pdf")
        used = await self._assets.total_bytes()
        if used >= self._settings.max_total_bytes:
            raise AssetQuotaExceededError  # skip the parse when already full

        data = await asyncio.to_thread(_read_all, file)
        try:
            processed = await asyncio.wait_for(
                asyncio.to_thread(inspect_pdf, io.BytesIO(data)),
                timeout=self._settings.document_scan_timeout_seconds,
            )
        except TimeoutError:
            # The worker thread cannot be stopped, but the request is not held.
            logger.warning("Document inspection timed out; refused as corrupt")
            raise DocumentInvalidError("corrupt") from None
        except DocumentInvalidError as exc:
            logger.info("Document refused: %s", exc.reason)
            raise
        sha256 = hashlib.sha256(data).hexdigest()
        existing = await self._assets.find_by_hash(AssetKind.DOCUMENT, sha256)
        if existing is not None:
            if existing.is_quarantined:
                raise DocumentQuarantinedError(existing.id)
            return UploadResult(existing, created=False)
        if used + len(data) > self._settings.max_total_bytes:
            raise AssetQuotaExceededError

        asset_id: uuid.UUID = uuid7()
        key = content_key(asset_id)
        await self._storage.put(key, data)
        try:
            record = await self._assets.create(
                AssetCreate(
                    id=asset_id,
                    kind=AssetKind.DOCUMENT,
                    mime="application/pdf",
                    size_bytes=len(data),
                    sha256=sha256,
                    title=clean_title,
                    page_count=processed.page_count,
                    uploaded_by_sub=uploaded_by_sub,
                    rights_declared_at=datetime.now(UTC),
                )
            )
        except AssetAlreadyExistsError:
            # Lost a race against an identical upload: use the winner's.
            await self._discard([key])
            winner = await self._assets.find_by_hash(AssetKind.DOCUMENT, sha256)
            if winner is None:
                raise
            if winner.is_quarantined:
                raise DocumentQuarantinedError(winner.id) from None
            return UploadResult(winner, created=False)
        except BaseException:
            await self._discard([key])
            raise
        logger.info(
            "Document %s uploaded by sub=%s, rights declared, %d page(s)",
            asset_id,
            uploaded_by_sub,
            processed.page_count,
        )
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
        cover: CoverUpload | None = None,
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
            cover=cover,
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
        cover: CoverUpload | None = None,
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
            cover=cover,
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
        cover: CoverUpload | None = None,
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
            if existing.is_quarantined:
                raise AudioQuarantinedError(existing.id)
            return UploadResult(existing, created=False)

        # The picture: the one the editor sent (any fault is theirs to fix), or
        # else the one inside the file (any fault just means no cover).
        prepared, warnings = await self._prepare_cover(file, cover)
        cover_bytes = 0
        if prepared is not None:
            twin = await self._assets.find_by_hash(
                AssetKind.COVER,
                hashlib.sha256(prepared.data).hexdigest(),
                library=library,
            )
            cover_bytes = 0 if twin else len(prepared.data)
        total = used + len(processed.data) + cover_bytes
        if not library and total > self._settings.max_total_bytes:
            raise AssetQuotaExceededError

        stored_cover: _StoredCover | None = None
        if prepared is not None:
            stored_cover = await self._store_cover(
                prepared,
                title=clean_title,
                uploaded_by_sub=uploaded_by_sub,
                rights_declared_at=datetime.now(UTC),
                library=library,
            )

        asset_id: uuid.UUID = uuid7()
        key = content_key(asset_id)
        try:
            await self._storage.put(key, processed.data)
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
                    cover_asset_id=stored_cover.record.id if stored_cover else None,
                )
            )
        except AssetAlreadyExistsError:
            # Lost a race against an identical upload: use the winner's.
            await self._discard([key])
            await self._drop_new_cover(stored_cover)
            winner = await self._assets.find_by_hash(
                AssetKind.AUDIO, sha256, library=library
            )
            if winner is None:
                raise
            if winner.is_quarantined:
                raise AudioQuarantinedError(winner.id) from None
            return UploadResult(winner, created=False)
        except BaseException:
            await self._discard([key])
            await self._drop_new_cover(stored_cover)
            raise
        return UploadResult(record, created=True, warnings=tuple(warnings))

    async def _prepare_cover(
        self, audio: BinaryIO, cover: CoverUpload | None
    ) -> tuple[ProcessedCover | None, list[str]]:
        """The cover to store, made by the cover pipeline, and the warnings.

        A cover the editor sent must be good: `CoverTooLargeError` or
        `CoverInvalidError` otherwise. Artwork found inside the audio never
        fails the upload: if it is too big, corrupt or hostile it is dropped
        and `cover_dropped` is reported."""
        if cover is not None:
            return await self._process_sent_cover(cover), []
        found = await asyncio.to_thread(
            find_artwork, audio, self._settings.max_cover_bytes
        )
        if found is None:
            return None, []
        if found.data is None:
            logger.info("Embedded artwork dropped: over the size limit")
            return None, ["cover_dropped"]
        try:
            processed = await asyncio.to_thread(
                process_cover, io.BytesIO(found.data), self._settings.image_limits
            )
        except CoverInvalidError as exc:
            logger.info("Embedded artwork dropped: %s", exc.reason)
            return None, ["cover_dropped"]
        except Exception:
            logger.warning(
                "Embedded artwork dropped: unexpected failure", exc_info=True
            )
            return None, ["cover_dropped"]
        return processed, []

    async def _process_sent_cover(self, cover: CoverUpload) -> ProcessedCover:
        limit = self._settings.max_cover_bytes
        if cover.size > limit:
            raise CoverTooLargeError(limit)
        if cover.size == 0:
            raise CoverInvalidError("the file is empty")
        try:
            return await asyncio.to_thread(
                process_cover, cover.file, self._settings.image_limits
            )
        except CoverInvalidError as exc:
            logger.info("Cover refused: %s", exc.reason)
            raise

    async def _store_cover(
        self,
        cover: ProcessedCover,
        *,
        title: str,
        uploaded_by_sub: str | None,
        rights_declared_at: datetime | None,
        library: bool,
    ) -> _StoredCover:
        """The cover as an asset of its own, or the identical one already there."""
        sha256 = hashlib.sha256(cover.data).hexdigest()
        existing = await self._assets.find_by_hash(
            AssetKind.COVER, sha256, library=library
        )
        if existing is not None:
            return _StoredCover(existing, created=False)
        cover_id: uuid.UUID = uuid7()
        key = content_key(cover_id)
        await self._storage.put(key, cover.data)
        try:
            record = await self._assets.create(
                AssetCreate(
                    id=cover_id,
                    kind=AssetKind.COVER,
                    mime="image/webp",
                    width=cover.width,
                    height=cover.height,
                    size_bytes=len(cover.data),
                    sha256=sha256,
                    title=title,
                    uploaded_by_sub=uploaded_by_sub,
                    rights_declared_at=rights_declared_at,
                )
            )
        except AssetAlreadyExistsError:
            await self._discard([key])
            winner = await self._assets.find_by_hash(
                AssetKind.COVER, sha256, library=library
            )
            if winner is None:
                raise
            return _StoredCover(winner, created=False)
        except BaseException:
            await self._discard([key])
            raise
        return _StoredCover(record, created=True)

    async def _drop_new_cover(self, stored: _StoredCover | None) -> None:
        """Undoes `_store_cover` when the track it was for was not stored."""
        if stored is not None and stored.created:
            await self._release_cover(stored.record.id)

    async def _release_cover(self, cover_id: uuid.UUID) -> None:
        """Deletes a cover no track points at any more (row first, then file).
        One that another track still uses is kept. Best effort: a cover that
        stays behind is collected as garbage later."""
        try:
            deleted = await self._assets.delete_if_unused(cover_id)
        except AssetInUseError:
            return
        except Exception:
            logger.warning("Could not release cover %s", cover_id, exc_info=True)
            return
        if deleted:
            await self._discard([content_key(cover_id)])

    async def set_cover(
        self,
        asset_id: uuid.UUID,
        file: BinaryIO,
        size: int,
        *,
        uploaded_by_sub: str | None,
    ) -> AssetRecord:
        """Replaces the cover of an audio track of this tenant. The new one is
        stored first and the track switches to it in one update, so there is
        never a moment with no cover; the old one is released afterwards."""
        self.ensure_uploads_enabled(AssetKind.COVER)
        track = await self._require_own_audio(asset_id)
        prepared = await self._process_sent_cover(CoverUpload(file, size))
        twin = await self._assets.find_by_hash(
            AssetKind.COVER, hashlib.sha256(prepared.data).hexdigest()
        )
        used = await self._assets.total_bytes()
        if not twin and used + len(prepared.data) > self._settings.max_total_bytes:
            raise AssetQuotaExceededError
        stored = await self._store_cover(
            prepared,
            title=track.title,
            uploaded_by_sub=uploaded_by_sub,
            # This request carries no declaration of its own.
            rights_declared_at=None,
            library=False,
        )
        try:
            switched = await self._assets.set_cover(asset_id, stored.record.id)
        except BaseException:
            await self._drop_new_cover(stored)
            raise
        if switched is None:
            await self._drop_new_cover(stored)
            raise AssetNotFoundError(str(asset_id))
        updated, previous = switched
        if previous is not None and previous != stored.record.id:
            await self._release_cover(previous)
        return updated

    async def remove_cover(self, asset_id: uuid.UUID) -> None:
        """Takes the cover off an audio track of this tenant (nothing to do if
        it has none) and releases it."""
        await self._require_own_audio(asset_id)
        switched = await self._assets.set_cover(asset_id, None)
        if switched is None:
            raise AssetNotFoundError(str(asset_id))
        if switched[1] is not None:
            await self._release_cover(switched[1])

    async def _require_own_audio(self, asset_id: uuid.UUID) -> AssetRecord:
        asset = await self._require_own(asset_id)
        if asset.kind is not AssetKind.AUDIO or asset.is_quarantined:
            raise AssetNotFoundError(str(asset_id))
        return asset

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
        present = await availability(self._storage, records)
        return AssetList(
            items=[
                AssetListItem.from_record_and_usage(
                    r, usage[r.id], available=present[r.id]
                )
                for r in records
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
            data.title is not None
            and asset.kind is not AssetKind.DOCUMENT
            and len(data.title) > TITLE_MAX_LENGTH
        ):
            raise InvalidAssetMetadataError(f"title over {TITLE_MAX_LENGTH}")
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
        asset = await self._require_own(asset_id)
        if not await self._assets.delete_if_unused(asset_id):
            raise AssetNotFoundError(str(asset_id))
        for key in (thumb_key(asset_id), content_key(asset_id)):
            try:
                await self._storage.delete(key)
            except Exception:  # noqa: BLE001  the asset is gone either way
                logger.error("Asset %s deleted but %s was not removed", asset_id, key)
        if asset.cover_asset_id is not None:
            # The track's cover goes with it, unless another track shares it.
            await self._release_cover(asset.cover_asset_id)

    async def get_content(self, raw_id: str, *, thumb: bool = False) -> AssetContent:
        """The bytes to serve. Anything that is not a known asset id is not found.

        `raw_id` comes straight from the URL: it must be a canonical UUID, so
        nothing resembling a path ever reaches the storage layer.
        """
        asset_id = _parse_asset_id(raw_id)
        asset = await self._assets.get_by_id(asset_id)
        if (
            asset is None
            or asset.is_quarantined  # taken down: as good as gone
            or (thumb and not asset.kind.has_thumbnail)
        ):
            raise AssetNotFoundError(raw_id)
        if asset.kind is AssetKind.COVER and not await self._assets.cover_in_service(
            asset_id
        ):
            # A cover lives and dies with its tracks: taking the track down
            # takes its picture down too.
            raise AssetNotFoundError(raw_id)
        key = thumb_key(asset_id) if thumb else content_key(asset_id)
        try:
            data = await self._storage.get(key)
        except AssetFileMissingError as exc:
            logger.error(
                "Asset %s: the file is missing from storage (key %s); the database "
                "lists it but the storage directory has no bytes for it. Check the "
                "volume mount of ASSETS_DIR and run `python -m app.scripts.assets "
                "verify`",
                asset_id,
                key,
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


def _clean_title(raw: str, limit: int = TITLE_MAX_LENGTH) -> str:
    cleaned = " ".join(raw.split())
    if not cleaned or len(cleaned) > limit:
        raise InvalidAssetMetadataError(f"title must be 1-{limit} chars")
    return cleaned


def _read_all(file: BinaryIO) -> bytes:
    file.seek(0)
    return file.read()


def _parse_asset_id(raw_id: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(raw_id)
    except ValueError as exc:
        raise AssetNotFoundError(raw_id) from exc
    if str(parsed) != raw_id.lower():  # braces, urn:, missing hyphens...
        raise AssetNotFoundError(raw_id)
    return parsed
