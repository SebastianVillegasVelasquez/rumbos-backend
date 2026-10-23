"""The content of a content bubble: structured blocks, or a widget.

Two tiers, decided on purpose:

- **blocks**: data. Every field is plain text or a closed value (a video is a
  provider plus an id, never a URL; a link is an https URL; an image is an
  asset id). Nothing in a block is ever HTML, and the frontend renders it with
  its own components, so there is nothing for the server to sanitize.
- **widget**: author-written HTML, CSS and JavaScript. The server does NOT parse
  or sanitize it, and must not pretend to: HTML sanitizers are an arms race and
  a half-sanitized widget is worse than an honest one. It is stored as an opaque
  string (checked for size and valid UTF-8 only) and the frontend runs it
  inside a sandboxed iframe, which is where the isolation really lives. That is
  why widgets are off for every tenant until an operator switches them on.

The wire format is camelCase and strict: unknown fields are rejected, and only
the camelCase spelling is accepted.
"""

import json
import re
import unicodedata
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

from app.exceptions import ContentInvalidError, ContentTooLargeError

MAX_BLOCKS = 40
MAX_BLOCKS_BODY_BYTES = 20 * 1024
MAX_WIDGET_HTML_BYTES = 200 * 1024
MAX_MAP_CONTENT_BYTES = 1024 * 1024

TITLE_MAX = 120
HEADING_MAX = 120
PARAGRAPH_MAX = 1500
ALT_MAX = 200
CAPTION_MAX = 200
LINK_TEXT_MAX = 80
VIDEO_TITLE_MAX = 120
WIDGET_ALT_MAX = 300
URL_MAX = 2000

# Format characters that reorder or hide text. Joiners (ZWJ, ZWNJ) stay: emoji
# and several scripts need them.
_HIDDEN_FORMAT = frozenset(
    "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff"
)


def clean_text(value: Any, *, multiline: bool) -> Any:
    """`value` as plain text: NFC, control characters gone, ends trimmed.

    Newlines survive only when `multiline` (a paragraph); elsewhere they, and
    tabs, become a space. Anything that is not a string is returned as is, for
    the type check that follows to refuse.
    """
    if not isinstance(value, str):
        return value
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for ch in text:
        if ch == "\n":
            kept.append("\n" if multiline else " ")
        elif ch == "\t":
            kept.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cs") or ch in _HIDDEN_FORMAT:
            continue
        else:
            kept.append(ch)
    return unicodedata.normalize("NFC", "".join(kept)).strip()


def _line(value: Any) -> Any:
    return clean_text(value, multiline=False)


def _paragraph(value: Any) -> Any:
    return clean_text(value, multiline=True)


Line = Annotated[StrictStr, BeforeValidator(_line)]
Paragraph = Annotated[StrictStr, BeforeValidator(_paragraph)]

_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,32}")
_URL_FORBIDDEN = frozenset('\\<>"`')


def _check_https_url(value: Any) -> Any:
    """Only an absolute https URL with a plain host and no credentials.

    Not trimmed or repaired: a URL with whitespace or control characters
    anywhere (`" https://x"`, `"java\\nscript:"`) is refused, not fixed.
    """
    if not isinstance(value, str):
        return value
    bad = PydanticCustomError("invalid_url", "Not a valid https URL")
    if not value or len(value) > URL_MAX:
        raise bad
    for ch in value:
        if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cs", "Cf"):
            raise bad
        if ch in _URL_FORBIDDEN:
            raise bad
    if not value[:8].lower() == "https://":
        raise bad
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise bad from None
    host = parts.hostname or ""
    if parts.scheme != "https" or "@" in parts.netloc or not _HOST.fullmatch(host):
        raise bad
    if port is not None and port == 0:
        raise bad
    return value


HttpsUrl = Annotated[StrictStr, BeforeValidator(_check_https_url)]


def _check_video_id(value: Any) -> Any:
    if isinstance(value, str) and not _VIDEO_ID.fullmatch(value):
        raise PydanticCustomError("invalid_video_id", "Not a valid video id")
    return value


VideoId = Annotated[StrictStr, BeforeValidator(_check_video_id)]


def _exact_int(value: Any) -> Any:
    """An int, not `True`, `2.0` or `"2"`, which Python would let pass as one."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PydanticCustomError("invalid_type", "This value has the wrong type")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=False, extra="forbid"
    )


class HeadingBlock(_Strict):
    type: Literal["heading"]
    text: Line = Field(min_length=1, max_length=HEADING_MAX)
    level: Annotated[Literal[2, 3], BeforeValidator(_exact_int)]


class ParagraphBlock(_Strict):
    type: Literal["paragraph"]
    text: Paragraph = Field(min_length=1, max_length=PARAGRAPH_MAX)


class ImageBlock(_Strict):
    type: Literal["image"]
    asset_id: uuid.UUID
    alt: Line = Field(min_length=1, max_length=ALT_MAX)
    caption: Line | None = Field(default=None, max_length=CAPTION_MAX)

    @field_validator("caption")
    @classmethod
    def _empty_caption_is_none(cls, value: str | None) -> str | None:
        return value or None


class VideoBlock(_Strict):
    type: Literal["video"]
    provider: Literal["youtube", "vimeo"]
    video_id: VideoId
    title: Line = Field(min_length=1, max_length=VIDEO_TITLE_MAX)


class LinkBlock(_Strict):
    type: Literal["link"]
    text: Line = Field(min_length=1, max_length=LINK_TEXT_MAX)
    url: HttpsUrl


class DividerBlock(_Strict):
    type: Literal["divider"]


Block = Annotated[
    HeadingBlock | ParagraphBlock | ImageBlock | VideoBlock | LinkBlock | DividerBlock,
    Field(discriminator="type"),
]

AutoOpen = Literal["never", "first_visit"]


class BlocksBody(_Strict):
    schema_version: Annotated[Literal[1], BeforeValidator(_exact_int)]
    blocks: list[Block] = Field(max_length=MAX_BLOCKS)


class BlocksContent(_Strict):
    type: Literal["blocks"]
    title: Line = Field(min_length=1, max_length=TITLE_MAX)
    auto_open: AutoOpen = "never"
    body: BlocksBody


def _check_html(value: Any) -> Any:
    """Opaque on purpose (see the module docstring): only that it is text that
    can be stored and served as UTF-8, and that it is not empty."""
    if not isinstance(value, str):
        return value
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PydanticCustomError(
            "invalid_utf8", "The HTML is not valid UTF-8 text"
        ) from None
    if "\x00" in value:
        raise PydanticCustomError("invalid_utf8", "The HTML is not valid UTF-8 text")
    if not value.strip():
        raise PydanticCustomError("too_short", "The HTML is empty")
    return value


class WidgetContent(_Strict):
    type: Literal["widget"]
    title: Line = Field(min_length=1, max_length=TITLE_MAX)
    auto_open: AutoOpen = "never"
    alt_text: Line = Field(min_length=1, max_length=WIDGET_ALT_MAX)
    # Never parsed, sanitized or rewritten by the server.
    html: Annotated[StrictStr, BeforeValidator(_check_html)]


Content = Annotated[BlocksContent | WidgetContent, Field(discriminator="type")]

_ADAPTER: TypeAdapter[BlocksContent | WidgetContent] = TypeAdapter(Content)

# What a client is told for each kind of failure; never the offending input.
_MESSAGES = {
    "required": "This field is required",
    "unknown_field": "This field is not allowed here",
    "too_short": "This text is too short",
    "too_long": "This text is too long",
    "too_many_blocks": f"At most {MAX_BLOCKS} blocks are allowed",
    "unknown_type": "Unknown type",
    "invalid_value": "This value is not allowed",
    "invalid_type": "This value has the wrong type",
    "invalid_url": "Not a valid https URL",
    "invalid_video_id": "Not a valid video id",
    "invalid_utf8": "The HTML is not valid UTF-8 text",
    "invalid_asset_id": "Not a valid asset id",
    "invalid": "Invalid value",
}

_CODE_OF = {
    "missing": "required",
    "extra_forbidden": "unknown_field",
    "string_too_short": "too_short",
    "string_too_long": "too_long",
    "too_long": "too_long",
    "union_tag_invalid": "unknown_type",
    "union_tag_not_found": "required",
    "literal_error": "invalid_value",
    "string_type": "invalid_type",
    "int_type": "invalid_type",
    "dict_type": "invalid_type",
    "list_type": "invalid_type",
    "model_type": "invalid_type",
    "uuid_type": "invalid_asset_id",
    "uuid_parsing": "invalid_asset_id",
    "uuid_from_str": "invalid_asset_id",
}


_CONTENT_TAGS = frozenset(("blocks", "widget"))
_BLOCK_TAGS = frozenset(("heading", "paragraph", "image", "video", "link", "divider"))


def _path(loc: tuple[int | str, ...]) -> str:
    """`body.blocks[2].url` from pydantic's location, which also names the
    branch of each discriminated union it went through (dropped here)."""
    out = ""
    after_index = False
    for position, part in enumerate(loc):
        if (position == 0 and part in _CONTENT_TAGS) or (
            after_index and part in _BLOCK_TAGS
        ):
            after_index = False
            continue
        after_index = isinstance(part, int)
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def _problems(error: ValidationError) -> list[dict[str, str]]:
    problems = []
    for item in error.errors(
        include_input=False, include_url=False, include_context=False
    ):
        loc = item["loc"]
        # A wrong block type reports the tag position; the error is about it.
        code = _CODE_OF.get(item["type"], item["type"])
        if item["type"] == "too_long" and loc and loc[-1] == "blocks":
            code = "too_many_blocks"
        if code not in _MESSAGES:
            code = "invalid"
        problems.append({"path": _path(loc), "code": code, "message": _MESSAGES[code]})
    return problems


def parse_content(raw: object) -> BlocksContent | WidgetContent:
    """The validated content, or `ContentInvalidError` listing every problem
    (path, code, message) and `ContentTooLargeError` when only a size is wrong.
    """
    try:
        content = _ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise ContentInvalidError(_problems(exc)) from exc
    if isinstance(content, WidgetContent):
        size = len(content.html.encode("utf-8"))
        if size > MAX_WIDGET_HTML_BYTES:
            raise ContentTooLargeError(MAX_WIDGET_HTML_BYTES, "bubble")
    else:
        body = json.dumps(
            content.body.model_dump(mode="json", by_alias=True, exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(body.encode("utf-8")) > MAX_BLOCKS_BODY_BYTES:
            raise ContentTooLargeError(MAX_BLOCKS_BODY_BYTES, "bubble")
    return content


def to_stored(content: BlocksContent | WidgetContent) -> dict[str, Any]:
    """The JSON that is stored and published: camelCase, absent optionals left
    out (so `caption` is never `null`)."""
    return content.model_dump(mode="json", by_alias=True, exclude_none=True)


def stored_size(stored: dict[str, Any]) -> int:
    """Bytes the content counts for against its map's budget."""
    text = json.dumps(stored, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return len(text.encode("utf-8"))


def image_asset_ids(stored: dict[str, Any] | None) -> list[uuid.UUID]:
    """The assets the image blocks of stored content point at, in order, once."""
    if not stored or stored.get("type") != "blocks":
        return []
    found: list[uuid.UUID] = []
    for block in stored["body"]["blocks"]:
        if block.get("type") == "image":
            asset_id = uuid.UUID(block["assetId"])
            if asset_id not in found:
                found.append(asset_id)
    return found
