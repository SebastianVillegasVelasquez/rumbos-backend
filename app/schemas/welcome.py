"""The welcome curtain of a map: the full-screen entry screen with a title, a
message and an optional welcome video.

Everything here is data the frontend renders with its own components, so text
is plain text and is never interpreted as HTML. The only placeholder a text may
carry is `{curso}` (the course name, filled in by the frontend); any other
`{...}` is refused so a typo never reaches students as literal braces. The video
is a provider plus an id, exactly like a video block of a content bubble, and
never a URL.
"""

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
)
from pydantic_core import PydanticCustomError

from app.schemas.base import ApiModel
from app.schemas.content import VideoId, VideoProvider, clean_text

TITLE_MAX = 120
MESSAGE_MAX = 300

PLACEHOLDER = "{curso}"
_ANY_PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def _title(value: Any) -> Any:
    return _check_placeholders(clean_text(value, multiline=False))


def _message(value: Any) -> Any:
    return _check_placeholders(clean_text(value, multiline=True))


def _check_placeholders(value: Any) -> Any:
    if isinstance(value, str) and _ANY_PLACEHOLDER.search(
        value.replace(PLACEHOLDER, "")
    ):
        raise PydanticCustomError(
            "welcome_invalid_placeholder",
            "Only the {curso} placeholder is allowed",
        )
    return value


Title = Annotated[StrictStr, BeforeValidator(_title)]
Message = Annotated[StrictStr, BeforeValidator(_message)]


class _Strict(ApiModel):
    model_config = ConfigDict(extra="forbid")


class WelcomeVideo(_Strict):
    provider: VideoProvider
    video_id: VideoId


class Welcome(_Strict):
    """Defaults describe a curtain nobody configured: shown, with the default
    title, no video, on the first visit only."""

    enabled: StrictBool = True
    title: Title | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    message: Message | None = Field(default=None, max_length=MESSAGE_MAX)
    video: WelcomeVideo | None = None
    play_on_enter: StrictBool = True
    show: Literal["first_visit", "always"] = "first_visit"

    @field_validator("message")
    @classmethod
    def _empty_message_is_none(cls, value: str | None) -> str | None:
        return value or None

    def is_default(self) -> bool:
        return self == Welcome()
