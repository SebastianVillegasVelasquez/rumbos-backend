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
from app.exceptions import AssetTooLargeError, InvalidUploadError
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.repositories.sqlalchemy.tenant_repository import SqlAlchemyTenantRepository
from app.schemas.asset import AssetList, AssetRead, AssetUpdate
from app.services.asset_service import AssetContent, AssetService, AssetSettings

router = APIRouter(prefix="/assets", tags=["assets"])

# Multipart framing (boundaries, the `kind` field, part headers) on top of the
# file itself.
_BODY_OVERHEAD_BYTES = 64 * 1024

_FORM_FIELDS = {"file", "kind", "title", "credit", "license", "rightsDeclared"}
_MAX_FORM_FIELDS = len(_FORM_FIELDS) - 1  # all but the file

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
                        "kind": {
                            "type": "string",
                            "enum": ["background", "bubble", "audio"],
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


@router.post(
    "",
    response_model=AssetRead,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": AssetRead}},
    openapi_extra=_UPLOAD_OPENAPI,
    dependencies=[Depends(editor_route)],
)
async def upload_asset(
    request: Request,
    response: Response,
    service: ServiceDep,
    settings: AssetSettingsDep,
    principal: PrincipalDep,
) -> AssetRead:
    """201 with the new asset, or 200 with the existing one when the same
    content of the same kind was already uploaded (its title and credit are
    left as they were). Optional form fields: `title`, `credit`.

    Audio (`kind=audio`) also needs `title`, `credit` and `rightsDeclared=true`
    (the uploader's declaration that they have the rights); `license` is
    optional."""
    service.ensure_uploads_enabled()  # before reading a byte of the body
    limit = settings.largest_upload_bytes + _BODY_OVERHEAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise AssetTooLargeError(settings.largest_upload_bytes)

    limited = Request(request.scope, _limited(request.receive, limit))
    try:
        form = await limited.form(max_files=1, max_fields=_MAX_FORM_FIELDS)
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
                "kind must be 'background', 'bubble' or 'audio'"
            ) from exc
        file.file.seek(0, 2)
        size = file.file.tell()
        raw_title, raw_credit = form.get("title"), form.get("credit")
        if kind is AssetKind.AUDIO:
            raw_license, rights = form.get("license"), form.get("rightsDeclared")
            result = await service.upload_audio(
                file.file,
                size,
                title=raw_title if isinstance(raw_title, str) else None,
                credit=raw_credit if isinstance(raw_credit, str) else None,
                license=raw_license if isinstance(raw_license, str) else None,
                rights_declared=rights == "true",
                uploaded_by_sub=principal.sub,
            )
            if not result.created:
                response.status_code = status.HTTP_200_OK
            return AssetRead.from_record(result.asset)
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
    return AssetRead.from_record(result.asset)


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


@router.api_route(
    "/{asset_id}",
    methods=["GET", "HEAD"],
    response_class=Response,
    dependencies=[Depends(public_route)],
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
