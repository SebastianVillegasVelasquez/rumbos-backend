"""The HTML of an `html` step: an allowlist, and a refusal when anything else
shows up.

Authors write this HTML, so it is untrusted. The server does not sanitize it
(a half-sanitized document is worse than an honest refusal); it parses it with
a real parser, checks every tag and attribute against a closed list, and
answers with the first thing that is not on it, named, so the editor can fix
it. The frontend still renders the result inside a sandboxed iframe without
scripts and under a Content-Security-Policy: that is the boundary, and this
check only keeps well-meaning mistakes and obvious attacks out early.

Two independent passes, because a parser and a browser can disagree about what
is markup: the stdlib `html.parser` (structure and attributes, after entity
decoding) and a plain scan of every `<` that a browser could read as a tag.
Either one finding something not allowed refuses the whole field.
"""

import base64
import binascii
import re
import unicodedata
import uuid
from html.parser import HTMLParser

from app.core.paths import ASSETS_PATH
from app.schemas.content import is_https_url

MAX_HTML_BYTES = 100 * 1024
MAX_DATA_IMAGE_BYTES = 200 * 1024
MAX_DEPTH = 100

ALLOWED_TAGS = frozenset(
    [
        "p",
        "br",
        "hr",
        "div",
        "span",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "s",
        "small",
        "mark",
        "sub",
        "sup",
        "blockquote",
        "q",
        "cite",
        "code",
        "pre",
        "abbr",
        "table",
        "caption",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "colgroup",
        "col",
        "figure",
        "figcaption",
        "img",
        "a",
        "details",
        "summary",
    ]
)
VOID_TAGS = frozenset({"br", "hr", "img", "col"})
# `href` and `src` are checked on their own (and only on `a` and `img`).
ALLOWED_ATTRIBUTES = frozenset(
    [
        "class",
        "id",
        "title",
        "lang",
        "dir",
        "role",
        "alt",
        "width",
        "height",
        "colspan",
        "rowspan",
        "scope",
        "start",
        "reversed",
        "open",
    ]
)

_ARIA = re.compile(r"aria-[a-z][a-z0-9-]*")
_ASSET_SRC = re.compile(
    re.escape(ASSETS_PATH)
    + r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/thumb)?"
)
_DATA_IMAGE = re.compile(
    r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/]+={0,2})"
)
_MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
    "gif": (b"GIF87a", b"GIF89a"),
}
# Every `<` a browser reads as the start of a tag, an end tag, a declaration, a
# comment or a processing instruction.
_TAG_OPEN = re.compile(r"<(?:(/?)([A-Za-z][^\s/>]*)|([!?]))")
_SAFE_NAME = re.compile(r"[a-z0-9:_-]{1,32}")


class _Refused(Exception):
    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def _named(raw: str) -> str:
    """A name safe to show back: attacker text never reaches the message."""
    lowered = raw.lower()
    return lowered if _SAFE_NAME.fullmatch(lowered) else "(invalid name)"


def check_html(html: str) -> str | None:
    """None when the HTML is allowed, else what is wrong with it (the first
    thing found, naming the tag or attribute)."""
    try:
        _check_characters(html)
        checker = _Checker()
        checker.feed(html)
        checker.close()
        _check_tag_opens(html)
    except _Refused as refusal:
        return refusal.what
    return None


def _check_characters(html: str) -> None:
    try:
        html.encode("utf-8")
    except UnicodeEncodeError:
        raise _Refused("the text is not valid UTF-8") from None
    for ch in html:
        if unicodedata.category(ch) == "Cc" and ch not in "\t\n\r\f":
            raise _Refused("a control character")


def _check_tag_opens(html: str) -> None:
    for match in _TAG_OPEN.finditer(html):
        if match.group(3):
            raise _Refused("a comment, declaration or processing instruction")
        name = match.group(2).lower()
        if name not in ALLOWED_TAGS:
            raise _Refused(f"the tag <{_named(name)}>")


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            raise _Refused(f"the tag <{_named(tag)}>")
        for name, value in attrs:
            _check_attribute(tag, name, value)
        if tag not in VOID_TAGS:
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise _Refused(f"nesting deeper than {MAX_DEPTH} levels")

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            raise _Refused(f"the closing tag </{_named(tag)}>")
        if tag not in VOID_TAGS:
            self._depth = max(0, self._depth - 1)

    def handle_comment(self, data: str) -> None:
        raise _Refused("a comment")

    def handle_decl(self, decl: str) -> None:
        raise _Refused("a declaration")

    def handle_pi(self, data: str) -> None:
        raise _Refused("a processing instruction")

    def unknown_decl(self, data: str) -> None:
        raise _Refused("a declaration")


def _check_attribute(tag: str, name: str, value: str | None) -> None:
    if name in ALLOWED_ATTRIBUTES or _ARIA.fullmatch(name):
        return
    if name == "href" and tag == "a":
        if value is None or not is_https_url(value):
            raise _Refused("a link on <a> that is not an https: URL")
        return
    if name == "src" and tag == "img":
        if value is None or not is_allowed_image(value):
            raise _Refused(
                "an image on <img> that is neither an asset of this site nor a "
                "small embedded PNG, JPEG, WebP or GIF"
            )
        return
    raise _Refused(f"the attribute {_named(name)} on <{_named(tag)}>")


def asset_ids_in(html: str) -> set[uuid.UUID]:
    """The assets an HTML step shows (`{ASSETS_PATH}/{id}` anywhere in it, so
    that a mention counts as a use: better to keep a file than to lose one)."""
    return {
        uuid.UUID(found.group(0).removeprefix(ASSETS_PATH + "/")[:36])
        for found in _ASSET_SRC.finditer(html)
    }


def is_allowed_image(value: str) -> bool:
    if _ASSET_SRC.fullmatch(value):
        return True
    match = _DATA_IMAGE.fullmatch(value)
    if match is None:
        return False
    kind, payload = match.groups()
    if len(payload) > MAX_DATA_IMAGE_BYTES * 4 // 3 + 4:
        return False
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(raw) > MAX_DATA_IMAGE_BYTES or not raw.startswith(_MAGIC[kind]):
        return False
    return kind != "webp" or raw[8:12] == b"WEBP"
