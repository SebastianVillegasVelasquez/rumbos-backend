"""The welcome curtain of a map: the full-screen entry screen with a title, a
message and an optional welcome video, or an ordered flow of steps that
replaces them.

Everything here is data the frontend renders with its own components, so text
is plain text and is never interpreted as HTML. The only placeholder a text may
carry is `{curso}` (the course name, filled in by the frontend); any other
`{...}` is refused so a typo never reaches students as literal braces. The video
is a provider plus an id, exactly like a video block of a content bubble, and
never a URL.

When `steps` is present it defines the flow and the legacy fields (`title`,
`message`, `video`, `playOnEnter`) must be null or absent. Per-student progress
is never stored here: it lives in Moodle.

This module holds the shapes and what a type can say (lengths, closed values).
The rules that need the whole flow or the author's HTML and CSS are checked at
write time by `app.welcome_flow.flow`; reads never re-run them, so a stored
flow always loads.
"""

import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.schemas.base import ApiModel
from app.schemas.content import Line, Paragraph, VideoId, VideoProvider, clean_text

TITLE_MAX = 120
MESSAGE_MAX = 300
MAX_STEPS = 12
MAX_QUIZ_STEPS = 3
STEP_TITLE_MAX = 120
BUTTON_LABEL_MAX = 40
ACK_LABEL_MAX = 160
QUIZ_TEXT_MAX = 300
MAX_MIN_SECONDS = 600
MAX_REVISION = 9999
# What `html` and `css` may weigh, in UTF-8 bytes, per field and over the flow.
MAX_HTML_BYTES = 100 * 1024
MAX_CSS_BYTES = 50 * 1024
MAX_FLOW_MARKUP_BYTES = 400 * 1024
STEP_KEY_PATTERN = r"^[a-z0-9]{6,16}$"

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


def _empty_is_none(value: str | None) -> str | None:
    return value or None


class Ack(_Strict):
    """The confirmation a student gives to pass a step ("I have read it")."""

    label: Line = Field(min_length=1, max_length=ACK_LABEL_MAX)


Seconds = Annotated[int, Field(strict=True, ge=0, le=MAX_MIN_SECONDS)]


class _StepBase(_Strict):
    key: StrictStr = Field(pattern=STEP_KEY_PATTERN)
    title: Line = Field(min_length=1, max_length=STEP_TITLE_MAX)
    mandatory: StrictBool = True
    revision: Annotated[int, Field(strict=True, ge=1, le=MAX_REVISION)] = 1


class WelcomeStep(_StepBase):
    """The greeting. At most one, and only as the first step."""

    type: Literal["welcome"]
    message: Message | None = Field(default=None, max_length=MESSAGE_MAX)
    button_label: Line | None = Field(default=None, max_length=BUTTON_LABEL_MAX)

    _no_empty = field_validator("message", "button_label")(_empty_is_none)


class VideoStep(_StepBase):
    type: Literal["video"]
    video: WelcomeVideo
    allow_fallback: StrictBool = True


class HtmlStep(_StepBase):
    """Author-written HTML and CSS, rendered by the frontend in a sandboxed,
    script-less iframe. Kept exactly as written: the write-time check refuses,
    never repairs."""

    type: Literal["html"]
    html: StrictStr = Field(min_length=1, max_length=MAX_HTML_BYTES)
    css: StrictStr | None = Field(default=None, max_length=MAX_CSS_BYTES)
    ack: Ack | None = None
    min_seconds: Seconds = 0

    _no_empty_css = field_validator("css")(_empty_is_none)


class PdfStep(_StepBase):
    """A document asset of the tenant (kind `document`)."""

    type: Literal["pdf"]
    asset_id: uuid.UUID
    ack: Ack | None = None
    min_seconds: Seconds = 0


class QuizStep(_StepBase):
    """A Moodle quiz the student takes outside Rumbos. `cmid` is null until the
    editor links one. Nothing about the attempt is ever stored here."""

    type: Literal["quiz"]
    cmid: Annotated[int, Field(strict=True, gt=0)] | None = None
    intro: Paragraph | None = Field(default=None, max_length=QUIZ_TEXT_MAX)
    outcome_above: Paragraph | None = Field(default=None, max_length=QUIZ_TEXT_MAX)
    outcome_below: Paragraph | None = Field(default=None, max_length=QUIZ_TEXT_MAX)

    _no_empty = field_validator("intro", "outcome_above", "outcome_below")(
        _empty_is_none
    )


Step = Annotated[
    WelcomeStep | VideoStep | HtmlStep | PdfStep | QuizStep,
    Field(discriminator="type"),
]


def _limit_step_count(value: Any) -> Any:
    # Before the steps are parsed one by one: a huge list is refused outright.
    if isinstance(value, list) and len(value) > MAX_STEPS:
        raise PydanticCustomError(
            "steps_too_many", "At most {limit} steps are allowed", {"limit": MAX_STEPS}
        )
    return value


Steps = Annotated[
    Annotated[list[Step], Field(min_length=1)] | None,
    BeforeValidator(_limit_step_count),
]


class Welcome(_Strict):
    """Defaults describe a curtain nobody configured: shown, with the default
    title, no video, on the first visit only.

    `steps` null or absent: the legacy fields define the curtain. `steps`
    present: it defines the flow, and `title`, `message`, `video` and
    `playOnEnter` are null (`play_on_enter` reads as None then). `show` applies
    to both."""

    enabled: StrictBool = True
    # Declared first so the legacy fields can tell which mode they are in.
    steps: Steps = None
    title: Title | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    message: Message | None = Field(default=None, max_length=MESSAGE_MAX)
    video: WelcomeVideo | None = None
    play_on_enter: StrictBool | None = True
    show: Literal["first_visit", "always"] = "first_visit"

    @field_validator("message")
    @classmethod
    def _empty_message_is_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("play_on_enter")
    @classmethod
    def _legacy_needs_a_value(
        cls, value: bool | None, info: ValidationInfo
    ) -> bool | None:
        # `null` is only meaningful (and only allowed) next to steps.
        if value is None and "steps" in info.data and info.data["steps"] is None:
            raise PydanticCustomError("bool_type", "This value has the wrong type")
        return value

    @model_validator(mode="after")
    def _steps_replace_playback(self) -> "Welcome":
        # A flow carries no legacy playback flag, unless the caller sent one
        # (which the write-time check then refuses).
        if self.steps is not None and "play_on_enter" not in self.model_fields_set:
            self.play_on_enter = None
        return self

    def is_default(self) -> bool:
        return self == Welcome()
