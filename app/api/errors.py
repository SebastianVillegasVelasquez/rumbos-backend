from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.exceptions import (
    BubbleNotFoundError,
    CourseMapAlreadyExistsError,
    CourseMapNotFoundError,
)

ExceptionHandler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def register_exception_handlers(app: FastAPI) -> None:
    """Map domain errors to HTTP responses."""

    def _handler(code: int, detail: str) -> ExceptionHandler:
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
            status.HTTP_409_CONFLICT, "A course map already exists for this course"
        ),
    )
