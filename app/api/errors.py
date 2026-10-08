from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.exceptions import (
    ActivityAlreadyPlacedError,
    AssetDimensionsTooLargeError,
    AssetInvalidImageError,
    AssetNotFoundError,
    AssetQuotaExceededError,
    AssetTooLargeError,
    AssetTypeNotAllowedError,
    BubbleNotFoundError,
    CourseMapNotFoundError,
    InvalidUploadError,
    OrderMismatchError,
    SectionAlreadyMappedError,
    SkinAssetNotFoundError,
    SkinAssetSizeMismatchError,
    SkinAssetWrongKindError,
    SkinIsBuiltinError,
    SkinNotFoundError,
    SkinReferenceNotFoundError,
    UploadsDisabledError,
)
from app.moodle.exceptions import (
    MoodleAuthError,
    MoodleCourseNotFoundError,
    MoodleError,
    MoodleUnavailableError,
)

ExceptionHandler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def register_exception_handlers(app: FastAPI) -> None:
    """Map domain errors to HTTP responses."""

    def _handler(code: int, detail: Any) -> ExceptionHandler:
        async def handle(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=code, content={"detail": detail})

        return handle

    app.add_exception_handler(
        CourseMapNotFoundError,
        _handler(status.HTTP_404_NOT_FOUND, "Course map not found"),
    )
    app.add_exception_handler(
        BubbleNotFoundError, _handler(status.HTTP_404_NOT_FOUND, "Bubble not found")
    )
    app.add_exception_handler(
        SectionAlreadyMappedError,
        _handler(
            status.HTTP_409_CONFLICT,
            {
                "code": "map_already_exists_for_section",
                "message": "This course already has a map for that section",
            },
        ),
    )

    async def activity_already_placed(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ActivityAlreadyPlacedError)
        detail: dict[str, Any] = {
            "code": "activity_already_placed",
            "message": "This activity already has a bubble on a map of the course",
        }
        if exc.course_map_id is not None:
            detail["courseMapId"] = str(exc.course_map_id)
            detail["courseMapTitle"] = exc.course_map_title
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": detail}
        )

    app.add_exception_handler(ActivityAlreadyPlacedError, activity_already_placed)

    async def order_mismatch(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, OrderMismatchError)
        detail = {
            "code": "order_mismatch",
            "message": "The ids must be exactly the existing items, each once",
            "missingIds": [str(i) for i in exc.missing],
            "unexpectedIds": [str(i) for i in exc.unexpected],
            "duplicatedIds": [str(i) for i in exc.duplicated],
        }
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": detail},
        )

    app.add_exception_handler(OrderMismatchError, order_mismatch)

    app.add_exception_handler(
        AssetNotFoundError, _handler(status.HTTP_404_NOT_FOUND, "Asset not found")
    )

    def _coded(
        code: int,
        name: str,
        message: str,
        extras: Callable[[Any], dict[str, Any]] | None = None,
    ) -> ExceptionHandler:
        """`detail = { code, message, ...extras }`, messages fixed on purpose
        (Pillow's own error text never reaches the client)."""

        async def handle(request: Request, exc: Exception) -> JSONResponse:
            detail: dict[str, Any] = {"code": name, "message": message}
            if extras is not None:
                detail.update(extras(exc))
            return JSONResponse(status_code=code, content={"detail": detail})

        return handle

    app.add_exception_handler(
        InvalidUploadError,
        _coded(
            422,
            "invalid_upload",
            "Send multipart/form-data with a `file` and a `kind` of "
            "'background' or 'bubble'",
        ),
    )
    app.add_exception_handler(
        UploadsDisabledError,
        _coded(503, "uploads_disabled", "Image uploads are disabled"),
    )
    app.add_exception_handler(
        AssetTooLargeError,
        _coded(
            413,
            "asset_too_large",
            "The file is too large",
            lambda exc: {"limitBytes": exc.limit_bytes},
        ),
    )
    app.add_exception_handler(
        AssetTypeNotAllowedError,
        _coded(
            422,
            "asset_type_not_allowed",
            "Only PNG, JPEG and static WebP images are accepted",
        ),
    )
    app.add_exception_handler(
        AssetInvalidImageError,
        _coded(422, "asset_invalid_image", "The file is not a valid image"),
    )
    app.add_exception_handler(
        AssetDimensionsTooLargeError,
        _coded(
            422,
            "asset_dimensions_too_large",
            "The image is too large in pixels",
            lambda exc: {
                "maxSide": exc.limit_side,
                "width": exc.width or None,
                "height": exc.height or None,
            },
        ),
    )
    app.add_exception_handler(
        AssetQuotaExceededError,
        _coded(507, "asset_quota_exceeded", "The storage quota is full"),
    )

    app.add_exception_handler(
        SkinNotFoundError, _handler(status.HTTP_404_NOT_FOUND, "Skin not found")
    )
    app.add_exception_handler(
        SkinIsBuiltinError,
        _coded(403, "skin_is_builtin", "Built-in skins cannot be changed or deleted"),
    )
    app.add_exception_handler(
        SkinReferenceNotFoundError,
        _coded(
            422,
            "skin_not_found",
            "A referenced skin does not exist",
            lambda exc: {"skinIds": [str(i) for i in exc.skin_ids]},
        ),
    )
    app.add_exception_handler(
        SkinAssetNotFoundError,
        _coded(
            422,
            "skin_asset_not_found",
            "A skin state refers to an asset that does not exist",
            lambda exc: {"states": {s: str(a) for s, a in exc.missing.items()}},
        ),
    )
    app.add_exception_handler(
        SkinAssetWrongKindError,
        _coded(
            422,
            "skin_asset_wrong_kind",
            "Skin states must use bubble images, not backgrounds",
            lambda exc: {"states": {s: str(a) for s, a in exc.wrong.items()}},
        ),
    )
    app.add_exception_handler(
        SkinAssetSizeMismatchError,
        _coded(
            422,
            "skin_asset_size_mismatch",
            "All state images of a skin must have the same width and height",
            lambda exc: {
                "sizes": [
                    {
                        "state": state,
                        "assetId": str(asset_id),
                        "width": width,
                        "height": height,
                    }
                    for state, asset_id, width, height in exc.sizes
                ]
            },
        ),
    )

    # Moodle failures. Messages are generic on purpose: never the token, nor
    # Moodle's own error text. Starlette picks the handler for the most
    # specific class, so the `MoodleError` fallback only catches the rest.
    app.add_exception_handler(
        MoodleUnavailableError,
        _handler(status.HTTP_503_SERVICE_UNAVAILABLE, "Moodle is unavailable"),
    )
    app.add_exception_handler(
        MoodleAuthError,
        _handler(status.HTTP_502_BAD_GATEWAY, "Moodle integration error"),
    )
    app.add_exception_handler(
        MoodleCourseNotFoundError,
        _handler(status.HTTP_404_NOT_FOUND, "Moodle course not found"),
    )
    app.add_exception_handler(
        MoodleError,
        _handler(status.HTTP_502_BAD_GATEWAY, "Moodle integration error"),
    )
