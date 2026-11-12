"""The CSS of an `html` step: tokenized (escapes decoded) and refused when it
reaches outside the document.

CSS is allowed broadly because the frontend renders it inside a sandboxed,
script-less iframe under a Content-Security-Policy; what is refused here is
what that sandbox could not contain by itself or what has no honest use in a
lesson: other documents (`@import`), network fetches (`url()` to anything but
an asset of this site or a small embedded image, remote fonts), the legacy
script hooks of old browsers (`expression()`, `behavior`, `-moz-binding`,
`javascript:`), a way out of the `<style>` element it may be placed in
(`</style>`), and anything unbalanced or unterminated, which is where parsers
disagree.

Escapes are decoded by the tokenizer (`tinycss2`), so `\\69mport` and
`e\\78pression(` are seen for what they are. A second, independent scan checks
that strings, comments and brackets are closed.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

import tinycss2

from app.welcome_flow.html import is_allowed_image

MAX_CSS_BYTES = 50 * 1024

_BLOCKS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = frozenset(_BLOCKS.values())
# Functions whose string arguments are URLs.
_URL_STRING_FUNCTIONS = frozenset(
    {"image-set", "-webkit-image-set", "image", "src", "cross-fade", "element"}
)
_FORBIDDEN_IDENTS = frozenset({"behavior", "-moz-binding", "-ms-behavior"})
_FORBIDDEN_FUNCTIONS = frozenset({"expression", "-moz-binding"})
# What is left of the text once comments are gone: legacy hooks that a comment
# inside the word would have hidden from the tokenizer (`expr/**/ession(`).
_LEGACY_HOOKS = re.compile(
    r"expression\s*\(|-moz-binding|(?<!scroll-)behavior\s*:|(?:java|vb)script\s*:",
    re.IGNORECASE,
)
_BREAKOUT = re.compile(r"</|<!", re.IGNORECASE)
_SCRIPT_SCHEME = re.compile(r"(?:java|vb)script:")


class _Refused(Exception):
    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def check_css(css: str) -> str | None:
    """None when the CSS is allowed, else what is wrong with it (the first
    thing found)."""
    try:
        _check_characters(css)
        stripped = _check_closed(css)
        if _BREAKOUT.search(css):
            raise _Refused("a sequence that could close the style element")
        rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
        _walk(rules, in_font_face=False)
        if _LEGACY_HOOKS.search(stripped):
            raise _Refused("a legacy script hook (expression, behavior, javascript:)")
    except _Refused as refusal:
        return refusal.what
    return None


def _check_characters(css: str) -> None:
    try:
        css.encode("utf-8")
    except UnicodeEncodeError:
        raise _Refused("the text is not valid UTF-8") from None
    for ch in css:
        if unicodedata.category(ch) == "Cc" and ch not in "\t\n\r\f":
            raise _Refused("a control character")


def _check_closed(css: str) -> str:
    """Every comment, string and bracket is closed. Returns the CSS without its
    comments (strings kept)."""
    out: list[str] = []
    stack: list[str] = []
    i, n = 0, len(css)
    while i < n:
        ch = css[i]
        if ch == "/" and css.startswith("/*", i):
            end = css.find("*/", i + 2)
            if end < 0:
                raise _Refused("an unterminated comment")
            i = end + 2  # dropped, so a word split by a comment is whole again
        elif ch in "\"'":
            end = _string_end(css, i)
            out.append(css[i:end])
            i = end
        elif ch == "\\":
            out.append(css[i : i + 2])
            i += 2
        else:
            if ch in _BLOCKS:
                stack.append(_BLOCKS[ch])
            elif ch in _CLOSERS and (not stack or stack.pop() != ch):
                raise _Refused("unbalanced brackets")
            out.append(ch)
            i += 1
    if stack:
        raise _Refused("unbalanced brackets")
    return "".join(out)


def _string_end(css: str, start: int) -> int:
    """Index just past the string that opens at `start`."""
    quote, i, n = css[start], start + 1, len(css)
    while i < n:
        ch = css[i]
        if ch == "\\":
            i += 2
        elif ch == quote:
            return i + 1
        elif ch in "\n\r\f":
            break
        else:
            i += 1
    raise _Refused("an unterminated string")


def _walk(nodes: Iterable[Any], *, in_font_face: bool) -> None:
    font_face_next = False
    for node in nodes:
        kind = node.type
        if kind == "error":
            raise _Refused("malformed CSS")
        if kind == "at-rule":
            _check_at_rule(node.lower_at_keyword)
            _walk(node.prelude, in_font_face=in_font_face)
            if node.content is not None:
                _walk(
                    node.content,
                    in_font_face=in_font_face or node.lower_at_keyword == "font-face",
                )
        elif kind == "at-keyword":
            _check_at_rule(node.lower_value)
            font_face_next = node.lower_value == "font-face"
        elif kind == "qualified-rule":
            _walk(node.prelude, in_font_face=in_font_face)
            _walk(node.content, in_font_face=in_font_face)
        elif kind == "{} block":
            _walk(node.content, in_font_face=in_font_face or font_face_next)
            font_face_next = False
        elif kind in ("() block", "[] block"):
            _walk(node.content, in_font_face=in_font_face)
        elif kind == "url":
            _check_url(node.value, in_font_face)
        elif kind == "function":
            _check_function(node, in_font_face)
        elif kind == "ident":
            if node.lower_value in _FORBIDDEN_IDENTS:
                raise _Refused(f"the property {node.lower_value}")
        elif kind == "string":
            _check_text(node.value)


def _check_at_rule(name: str) -> None:
    if name == "import":
        raise _Refused("@import")


def _check_function(node: Any, in_font_face: bool) -> None:
    name = node.lower_name
    if name in _FORBIDDEN_FUNCTIONS:
        raise _Refused(f"{name}()")
    if name == "url" or name in _URL_STRING_FUNCTIONS:
        for argument in node.arguments:
            if argument.type == "string":
                _check_url(argument.value, in_font_face)
    _walk(node.arguments, in_font_face=in_font_face)


def _check_url(value: str, in_font_face: bool) -> None:
    _check_text(value)
    if in_font_face:
        if not value.lower().startswith("data:"):
            raise _Refused("an @font-face source that is not a data: URL")
    elif not is_allowed_image(value):
        raise _Refused(
            "a url() that is neither an asset of this site nor a small embedded "
            "PNG, JPEG, WebP or GIF"
        )


def _check_text(value: str) -> None:
    squashed = "".join(
        ch for ch in value.lower() if not ch.isspace() and ord(ch) >= 0x20
    )
    if _SCRIPT_SCHEME.search(squashed):
        raise _Refused("a javascript: URL")
