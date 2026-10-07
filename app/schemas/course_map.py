import uuid
from datetime import datetime
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, StringConstraints, model_validator

from app.schemas.base import ApiModel
from app.schemas.bubble import BubbleRead

TITLE_MAX_LENGTH = 120
IMAGE_URL_MAX_LENGTH = 2048


def _check_image_url(value: str) -> str:
    """An absolute http(s) URL, or a site-relative path starting with `/`.

    Anything else (`javascript:`, `data:`, `file:`, bare words) is rejected,
    since the frontend puts this value in an `<img src>` / CSS `url()`.
    `//host/x` (and a slash followed by a backslash) is a scheme-relative URL
    in browsers, so it is rejected too: it would let a "path" point at another
    origin.
    """
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("imageUrl must not contain whitespace or control characters")
    if value.startswith("/"):
        if value.startswith(("//", "/\\")):
            raise ValueError("imageUrl must not be a scheme-relative URL")
        return value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(
            "imageUrl must be an absolute http(s) URL or a path starting with '/'"
        )
    return value


Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_LENGTH),
]
ImageUrl = Annotated[
    str,
    StringConstraints(max_length=IMAGE_URL_MAX_LENGTH),
    AfterValidator(_check_image_url),
]


class CourseMapCreate(ApiModel):
    title: Title
    moodle_course_id: int
    image_url: ImageUrl


class CourseMapUpdate(ApiModel):
    """Partial update: only fields present in the request are applied."""

    title: Title | None = None
    image_url: ImageUrl | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        sent = self.model_fields_set
        if not sent:
            raise ValueError("at least one field must be provided")
        for name in sent:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class CourseMapBase(ApiModel):
    """The `course_maps` row: what repositories return. Never has bubbles."""

    id: uuid.UUID
    title: str
    moodle_course_id: int
    image_url: str
    created_at: datetime
    updated_at: datetime


class CourseMapSummary(CourseMapBase):
    """A map as listed: counts its bubbles instead of carrying them."""

    bubble_count: int


class CourseMapList(ApiModel):
    items: list[CourseMapSummary]
    total: int  # matches of the filters, ignoring limit/offset
    limit: int
    offset: int


class CourseMapRead(CourseMapBase):
    """A course map together with its bubbles."""

    bubbles: list[BubbleRead]
