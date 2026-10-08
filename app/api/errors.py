from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.exceptions import (
    ActivityAlreadyPlacedError,
    BubbleNotFoundError,
    CourseMapNotFoundError,
    OrderMismatchError,
    SectionAlreadyMappedError,
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
