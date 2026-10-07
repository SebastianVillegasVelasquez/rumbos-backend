from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.exceptions import (
    BubbleNotFoundError,
    CourseMapAlreadyExistsError,
    CourseMapNotFoundError,
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
        CourseMapAlreadyExistsError,
        _handler(
            status.HTTP_409_CONFLICT,
            {
                "code": "map_already_exists_for_course",
                "message": "A course map already exists for this course",
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
