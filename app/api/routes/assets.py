"""Upload and serving of user-provided images.

Uploading, listing, editing and deleting are for editors, inside their own
tenant's library, and each tenant has its own storage quota. `GET /assets/{id}`
and its thumbnail are public by design: an `<img>` or a canvas cannot send an
`Authorization` header, so the unguessable id (a random UUID) is the only
protection. Whoever knows an image's id can fetch it; ids are only shown to the
tenant's editors and inside the maps its students may read.
"""

import dataclasses
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.datastructures import UploadFile
from starlette.types import Message

from app.assets.fingerprint import StorageState, storage_state_of
from app.assets.storage import AssetStorage, LocalDiskAssetStorage
from app.auth.dependencies import (
    PrincipalDep,
    ScopedSession,
    SystemSession,
    editor_route,
    public_route,
)
from app.core.config import Settings, get_settings
from app.enums import AssetKind, AssetScope
from app.exceptions import (
    AssetTooLargeError,
    CoverTooLargeError,
    InvalidUploadError,
    StorageUnavailableError,
)
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.schemas.asset import AssetList, AssetRead, AssetUpdate, AssetUploadRead
from app.services.asset_service import (
    AssetContent,
    AssetService,
    AssetSettings,
    CoverUpload,
)

router = APIRouter(prefix="/assets", tags=["assets"])

# Multipart framing (boundaries, the `kind` field, part headers) on top of the
# file itself.
_BODY_OVERHEAD_BYTES = 64 * 1024

_FORM_FIELDS = {
    "file",
    "cover",
    "kind",
    "title",
    "credit",
    "license",
    "rightsDeclared",
}
_MAX_FORM_FIELDS = len(_FORM_FIELDS) - 2  # all but the two files

_CACHE_CONTROL = "public, max-age=31536000, immutable"
# An image response must never be able to run anything, whatever it contains.
_CONTENT_SECURITY_POLICY = "default-src 'none'; sandbox"


def get_asset_settings(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssetSettings:
    return AssetSettings.from_app_settings(settings)


def get_asset_storage(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssetStorage:
    return LocalDiskAssetStorage(Path(settings.assets_dir))


AssetSettingsDep = Annotated[AssetSettings, Depends(get_asset_settings)]


async def get_asset_service(
    session: ScopedSession,
    principal: PrincipalDep,
    storage: Annotated[AssetStorage, Depends(get_asset_storage)],
    settings: AssetSettingsDep,
) -> AssetService:
    """The tenant's library. Its quota replaces the global default."""
    tenant = await SqlAlchemyTenantRepository(session).get_by_id(principal.tenant_id)
    assert tenant is not None  # the principal's tenant exists (a foreign key)
    own = dataclasses.replace(settings, max_total_bytes=tenant.asset_quota_bytes)
    return AssetService(SqlAlchemyAssetRepository(session), storage, own)


def get_public_asset_service(
    session: SystemSession,
    storage: Annotated[AssetStorage, Depends(get_asset_storage)],
    settings: AssetSettingsDep,
) -> AssetService:
    """Serving only: an image by its id, whichever tenant it belongs to."""
    return AssetService(SqlAlchemyAssetRepository(session), storage, settings)


ServiceDep = Annotated[AssetService, Depends(get_asset_service)]
PublicServiceDep = Annotated[AssetService, Depends(get_public_asset_service)]


class _BodyTooLargeError(Exception):
    pass


def _limited(
    receive: Callable[[], Awaitable[Message]], limit: int
) -> Callable[[], Awaitable[Message]]:
    """Wraps ASGI `receive` so reading more than `limit` bytes of body fails.

    The multipart parser pulls the body chunk by chunk and spools it to a
    temporary file, so this is what stops an endless upload: it is cut as soon
    as it passes the cap, never read whole into memory.
    """
    total = 0

    async def receive_limited() -> Message:
        nonlocal total
        message = await receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > limit:
                raise _BodyTooLargeError
        return message

    return receive_limited


_UPLOAD_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file", "kind"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        # Audio only; without one the artwork inside the file is used.
                        "cover": {"type": "string", "format": "binary"},
                        "kind": {
                            "type": "string",
                            "enum": ["background", "bubble", "image", "audio"],
                        },
                        "title": {"type": "string", "maxLength": 120},
                        "credit": {"type": "string", "maxLength": 300},
                        # Audio only.
                        "license": {"type": "string", "maxLength": 120},
                        "rightsDeclared": {"type": "string", "enum": ["true"]},
                    },
                }
            }
        },
    }
}


_COVER_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                }
            }
        },
    }
}


@router.post(
    "",
    response_model=AssetUploadRead,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": AssetUploadRead}},
    openapi_extra=_UPLOAD_OPENAPI,
    dependencies=[Depends(editor_route)],
)
async def upload_asset(
    request: Request,
    response: Response,
    service: ServiceDep,
    settings: AssetSettingsDep,
    principal: PrincipalDep,
) -> AssetUploadRead:
    """201 with the new asset, or 200 with the existing one when the same
    content of the same kind was already uploaded (its title and credit are
    left as they were). Optional form fields: `title`, `credit`.

    Audio (`kind=audio`) also needs `title`, `credit` and `rightsDeclared=true`
    (the uploader's declaration that they have the rights); `license` is
    optional. An audio upload may carry a `cover` file (PNG, JPEG or WebP); with
    none, the picture inside the audio is used if there is one, and
    `warnings` says `cover_dropped` when it could not be."""
    # Before reading a byte of the body.
    if storage_state_of(request.app) is StorageState.DEGRADED:
        raise StorageUnavailableError
    service.ensure_uploads_enabled()
    limit = settings.largest_body_bytes + _BODY_OVERHEAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise AssetTooLargeError(settings.largest_upload_bytes)

    limited = Request(request.scope, _limited(request.receive, limit))
    try:
        form = await limited.form(max_files=2, max_fields=_MAX_FORM_FIELDS)
    except _BodyTooLargeError as exc:
        raise AssetTooLargeError(settings.largest_upload_bytes) from exc
    try:
        if set(form.keys()) - _FORM_FIELDS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unexpected form field")
        file, raw_kind = form.get("file"), form.get("kind")
        if not isinstance(file, UploadFile):
            raise InvalidUploadError("missing file")
        try:
            kind = AssetKind(raw_kind if isinstance(raw_kind, str) else "")
        except ValueError as exc:
            raise InvalidUploadError(
                "kind must be 'background', 'bubble', 'image' or 'audio'"
            ) from exc
        if kind is AssetKind.COVER:  # made from an audio track, never sent alone
            raise InvalidUploadError(
                "kind must be 'background', 'bubble', 'image' or 'audio'"
            )
        raw_cover = form.get("cover")
        if raw_cover is not None and kind is not AssetKind.AUDIO:
            raise InvalidUploadError("a cover can only come with an audio track")
        file.file.seek(0, 2)
        size = file.file.tell()
        raw_title, raw_credit = form.get("title"), form.get("credit")
        if kind is AssetKind.AUDIO:
            raw_license, rights = form.get("license"), form.get("rightsDeclared")
            cover = None
            if raw_cover is not None:
                if not isinstance(raw_cover, UploadFile):
                    raise InvalidUploadError("cover must be a file")
                raw_cover.file.seek(0, 2)
                cover = CoverUpload(raw_cover.file, raw_cover.file.tell())
            result = await service.upload_audio(
                file.file,
                size,
                title=raw_title if isinstance(raw_title, str) else None,
                credit=raw_credit if isinstance(raw_credit, str) else None,
                license=raw_license if isinstance(raw_license, str) else None,
                rights_declared=rights == "true",
                uploaded_by_sub=principal.sub,
                cover=cover,
            )
            if not result.created:
                response.status_code = status.HTTP_200_OK
            return AssetUploadRead.from_upload(result.asset, result.warnings)
        result = await service.upload(
            file.file,
            size,
            kind,
            title=raw_title if isinstance(raw_title, str) else None,
            credit=raw_credit if isinstance(raw_credit, str) else None,
            filename=file.filename,
        )
    finally:
        await form.close()
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return AssetUploadRead.from_upload(result.asset)


@router.get("", response_model=AssetList, dependencies=[Depends(editor_route)])
async def list_assets(
    service: ServiceDep,
    kind: AssetKind | None = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
    scope: AssetScope = AssetScope.TENANT,
) -> AssetList:
    """`scope=tenant` (default): this tenant's own uploads. `scope=library`:
    the operator's audio library, readable by every tenant."""
    return await service.list_assets(
        kind=kind, q=q, limit=limit, offset=offset, scope=scope
    )


@router.patch(
    "/{asset_id}", response_model=AssetRead, dependencies=[Depends(editor_route)]
)
async def update_asset(
    asset_id: uuid.UUID, data: AssetUpdate, service: ServiceDep
) -> AssetRead:
    return AssetRead.from_record(await service.update_metadata(asset_id, data))


@router.delete(
    "/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(editor_route)],
)
async def delete_asset(asset_id: uuid.UUID, service: ServiceDep) -> Response:
    """409 `asset_in_use` while a map, skin or publication references it."""
    await service.delete(asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{asset_id}/cover",
    response_model=AssetRead,
    openapi_extra=_COVER_OPENAPI,
    dependencies=[Depends(editor_route)],
)
async def put_cover(
    asset_id: uuid.UUID,
    request: Request,
    service: ServiceDep,
    settings: AssetSettingsDep,
    principal: PrincipalDep,
) -> AssetRead:
    """Sets or replaces the cover of one of this tenant's audio tracks (form
    field `file`: PNG, JPEG or WebP). The old cover is released. 404 for a
    track that is not this tenant's, 403 for a library track."""
    if storage_state_of(request.app) is StorageState.DEGRADED:
        raise StorageUnavailableError
    service.ensure_uploads_enabled(AssetKind.COVER)
    limit = settings.max_cover_bytes + _BODY_OVERHEAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise CoverTooLargeError(settings.max_cover_bytes)

    limited = Request(request.scope, _limited(request.receive, limit))
    try:
        form = await limited.form(max_files=1, max_fields=0)
    except _BodyTooLargeError as exc:
        raise CoverTooLargeError(settings.max_cover_bytes) from exc
    try:
        file = form.get("file")
        if set(form.keys()) != {"file"} or not isinstance(file, UploadFile):
            raise InvalidUploadError("send the cover as the form file 'file'")
        file.file.seek(0, 2)
        size = file.file.tell()
        track = await service.set_cover(
            asset_id, file.file, size, uploaded_by_sub=principal.sub
        )
    finally:
        await form.close()
    return AssetRead.from_record(track)


@router.delete(
    "/{asset_id}/cover",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(editor_route)],
)
async def delete_cover(asset_id: uuid.UUID, service: ServiceDep) -> Response:
    """Takes the cover off one of this tenant's audio tracks (and deletes it
    unless another track shares it). Nothing to do if it has none."""
    await service.remove_cover(asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = [c.strip().removeprefix("W/") for c in header.split(",")]
    return "*" in candidates or etag in candidates


# One range, the only kind a player asks for: `bytes=0-99`, `bytes=100-`, `bytes=-500`.
_RANGE = re.compile(r"bytes=([0-9]{0,18})-([0-9]{0,18})")


def _byte_range(header: str, size: int) -> tuple[int, int] | None | Literal["bad"]:
    """The inclusive `(first, last)` bytes a `Range` header asks for.

    None: serve the whole file (the header is not one we honour, which RFC 9110
    allows: another unit, several ranges, or nonsense). "bad": it is a range
    but nothing of the file lies in it (416).
    """
    match = _RANGE.fullmatch(header.strip())
    if match is None:
        return None
    start, end = match.groups()
    if not start and not end:
        return None
    if not start:  # the last `end` bytes
        wanted = int(end)
        return "bad" if wanted == 0 else (max(0, size - wanted), size - 1)
    first = int(start)
    last = int(end) if end else size - 1
    if end and last < first:
        return None  # not a range at all
    if first >= size:
        return "bad"
    return (first, min(last, size - 1))


async def _serve(request: Request, content: AssetContent) -> Response:
    size = len(content.data)
    headers = {
        "Cache-Control": _CACHE_CONTROL,
        "ETag": content.etag,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
        # Players (iOS Safari above all) refuse to play what they cannot seek in.
        "Accept-Ranges": "bytes",
    }
    if _etag_matches(request.headers.get("if-none-match"), content.etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    wanted = request.headers.get("range")
    # `If-Range` makes a range conditional on the file being the one the client
    # holds; otherwise it gets the whole current file.
    if_range = request.headers.get("if-range")
    if wanted is not None and (if_range is None or if_range.strip() == content.etag):
        picked = _byte_range(wanted, size)
        if picked == "bad":
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        if picked is not None:
            first, last = picked
            return Response(
                content=content.data[first : last + 1],
                status_code=status.HTTP_206_PARTIAL_CONTENT,
                media_type=content.mime,
                headers={**headers, "Content-Range": f"bytes {first}-{last}/{size}"},
            )
    # The type comes from the database (what we wrote), never from a file name.
    if request.method == "HEAD":
        return Response(
            media_type=content.mime, headers={**headers, "Content-Length": str(size)}
        )
    return Response(content=content.data, media_type=content.mime, headers=headers)


@router.head(
    "/{asset_id}",
    response_class=Response,
    include_in_schema=False,  # the same operation as GET, without the body
    dependencies=[Depends(public_route)],
)
@router.get(
    "/{asset_id}", response_class=Response, dependencies=[Depends(public_route)]
)
async def get_asset(
    asset_id: str, request: Request, service: PublicServiceDep
) -> Response:
    """The bytes. Honours `Range` (206), `If-Range` and `If-None-Match` (304).
    Audio players need the range support."""
    # `asset_id` stays a plain string: anything that is not a canonical UUID
    # (including path-traversal attempts) is a 404, not a validation error.
    return await _serve(request, await service.get_content(asset_id))


@router.get(
    "/{asset_id}/thumb", response_class=Response, dependencies=[Depends(public_route)]
)
async def get_asset_thumbnail(
    asset_id: str, request: Request, service: PublicServiceDep
) -> Response:
    return await _serve(request, await service.get_content(asset_id, thumb=True))
