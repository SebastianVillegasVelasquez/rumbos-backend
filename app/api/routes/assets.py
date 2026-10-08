"""Upload and serving of user-provided images.

SECURITY: there is no authentication yet, so `POST /assets` is anonymous.
`ASSETS_MAX_TOTAL_BYTES` and `ASSETS_UPLOADS_ENABLED` only limit the damage;
they are not protection. Keep uploads disabled on any publicly reachable
deployment until auth exists. (`GET` is public by design: assets are shown to
learners.)
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.types import Message

from app.assets.storage import AssetStorage, LocalDiskAssetStorage
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.enums import AssetKind
from app.exceptions import AssetTooLargeError, InvalidUploadError
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.schemas.asset import AssetRead
from app.services.asset_service import AssetContent, AssetService, AssetSettings

router = APIRouter(prefix="/assets", tags=["assets"])

# Multipart framing (boundaries, the `kind` field, part headers) on top of the
# file itself.
_BODY_OVERHEAD_BYTES = 64 * 1024

_CACHE_CONTROL = "public, max-age=31536000, immutable"
# An image response must never be able to run anything, whatever it contains.
_CONTENT_SECURITY_POLICY = "default-src 'none'; sandbox"


def get_asset_settings(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssetSettings:
    return AssetSettings(
        uploads_enabled=settings.assets_uploads_enabled,
        max_total_bytes=settings.assets_max_total_bytes,
        max_background_bytes=settings.assets_max_background_bytes,
        max_bubble_bytes=settings.assets_max_bubble_bytes,
        max_background_side=settings.assets_max_background_side,
        max_bubble_side=settings.assets_max_bubble_side,
    )


def get_asset_storage(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AssetStorage:
    return LocalDiskAssetStorage(Path(settings.assets_dir))


AssetSettingsDep = Annotated[AssetSettings, Depends(get_asset_settings)]


def get_asset_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[AssetStorage, Depends(get_asset_storage)],
    settings: AssetSettingsDep,
) -> AssetService:
    return AssetService(SqlAlchemyAssetRepository(session), storage, settings)


ServiceDep = Annotated[AssetService, Depends(get_asset_service)]


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
                        "kind": {"type": "string", "enum": ["background", "bubble"]},
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
)
async def upload_asset(
    request: Request,
    response: Response,
    service: ServiceDep,
    settings: AssetSettingsDep,
) -> AssetRead:
    """201 with the new asset, or 200 with the existing one when the same
    content of the same kind was already uploaded."""
    service.ensure_uploads_enabled()  # before reading a byte of the body
    limit = settings.largest_upload_bytes + _BODY_OVERHEAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise AssetTooLargeError(settings.largest_upload_bytes)

    limited = Request(request.scope, _limited(request.receive, limit))
    try:
        form = await limited.form(max_files=1, max_fields=1)
    except _BodyTooLargeError as exc:
        raise AssetTooLargeError(settings.largest_upload_bytes) from exc
    try:
        file, raw_kind = form.get("file"), form.get("kind")
        if not isinstance(file, UploadFile):
            raise InvalidUploadError("missing file")
        try:
            kind = AssetKind(raw_kind if isinstance(raw_kind, str) else "")
        except ValueError as exc:
            raise InvalidUploadError("kind must be 'background' or 'bubble'") from exc
        file.file.seek(0, 2)
        size = file.file.tell()
        result = await service.upload(file.file, size, kind)
    finally:
        await form.close()
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return AssetRead.from_record(result.asset)


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = [c.strip().removeprefix("W/") for c in header.split(",")]
    return "*" in candidates or etag in candidates


async def _serve(request: Request, content: AssetContent) -> Response:
    headers = {
        "Cache-Control": _CACHE_CONTROL,
        "ETag": content.etag,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
    }
    if _etag_matches(request.headers.get("if-none-match"), content.etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    # The type comes from the database (what we wrote), never from a file name.
    return Response(content=content.data, media_type=content.mime, headers=headers)


@router.get("/{asset_id}", response_class=Response)
async def get_asset(asset_id: str, request: Request, service: ServiceDep) -> Response:
    # `asset_id` stays a plain string: anything that is not a canonical UUID
    # (including path-traversal attempts) is a 404, not a validation error.
    return await _serve(request, await service.get_content(asset_id))


@router.get("/{asset_id}/thumb", response_class=Response)
async def get_asset_thumbnail(
    asset_id: str, request: Request, service: ServiceDep
) -> Response:
    return await _serve(request, await service.get_content(asset_id, thumb=True))
