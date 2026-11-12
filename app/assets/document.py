"""Validation of uploaded PDF documents. Pure and synchronous.

Run it in a worker thread with a timeout (`asyncio.to_thread` and
`asyncio.wait_for`): parsing a hostile file can take long, and it must not
block the event loop.

A document is shown to students inside the viewer, so the question is not "is
this a pretty PDF" but "can it do anything besides show pages". Nothing is
rendered and no page content is ever decoded; the file is read as a graph of
objects (`pypdf`) and refused when it:

- does not start with `%PDF-`, is encrypted, or does not parse strictly
  (truncated, a broken cross-reference table, a loop in the page tree);
- has more than `MAX_PAGES` pages, by its own count or by walking its tree;
- carries active content: JavaScript, launch actions, embedded files, rich
  media, form submission or import, jumps into other files, additional
  actions (`/AA`), screen, movie, sound and 3D annotations. Every object of the
  file is checked, reachable or not, including those inside object streams
  (the only streams that are decompressed, because that is where objects are),
  and names are compared after their `#xx` escapes are decoded, so
  `/J#61vaScript` is `/JavaScript`. Plain `URI` links stay allowed.

The file is stored exactly as uploaded: the bytes are neither rewritten nor
compressed, and the metadata they carry (author, title, producer, dates) is
NOT stripped.
"""

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
)

from app.exceptions import DocumentInvalidError

MAX_PAGES = 200
# A 10 MB file cannot honestly hold more objects than this.
MAX_OBJECTS = 200_000
_MAX_DEPTH = 64
_MAX_PAGE_TREE_NODES = 20_000

# Keys and names that make a document do something.
_FORBIDDEN_NAMES = frozenset(
    {
        "/JavaScript",
        "/JS",
        "/AA",
        "/Launch",
        "/EmbeddedFile",
        "/EmbeddedFiles",
        "/EF",
        "/RichMedia",
        "/RichMediaContent",
        "/RichMediaSettings",
        "/SubmitForm",
        "/ImportData",
        "/GoToR",
        "/GoToE",
        "/XFA",
        "/Rendition",
        "/Movie",
        "/Sound",
        "/Screen",
        "/3D",
        "/FileAttachment",
    }
)

_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_STR_NAME_ESCAPE = re.compile(r"#([0-9A-Fa-f]{2})")
_RAW_NAME = re.compile(rb"/[^\s/<>\[\]()%{}]+")
_PAREN_OR_BACKSLASH = re.compile(rb"[()\\]")


@dataclass(frozen=True, slots=True)
class ProcessedDocument:
    page_count: int


def inspect_pdf(file: BinaryIO, max_pages: int = MAX_PAGES) -> ProcessedDocument:
    """Raises `DocumentInvalidError` (reason `not_pdf`, `encrypted`,
    `active_content`, `too_many_pages` or `corrupt`); otherwise the page
    count. The file is read, never changed."""
    file.seek(0)
    data = file.read()
    file.seek(0)
    if not data.startswith(b"%PDF-"):
        raise DocumentInvalidError("not_pdf")
    try:
        return _inspect(data, max_pages)
    except DocumentInvalidError:
        raise
    except Exception as exc:  # a parser can fail in many ways on hostile input
        raise DocumentInvalidError("corrupt") from exc


def _inspect(data: bytes, max_pages: int) -> ProcessedDocument:
    # Names in the raw text first: it is cheap and needs no parse, so a file
    # that is broken AND hostile is still reported for what it carries.
    if _raw_active_content(data):
        raise DocumentInvalidError("active_content")
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise DocumentInvalidError("encrypted")
    root = reader.trailer["/Root"]
    declared = _declared_page_count(root)
    if declared is not None and declared > max_pages:
        raise DocumentInvalidError("too_many_pages")
    counted = _count_pages(root, max_pages)
    if counted == 0:
        raise DocumentInvalidError("corrupt")
    _refuse_active_objects(reader)
    return ProcessedDocument(page_count=counted)


def _decoded(name: str) -> str:
    return _STR_NAME_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), name)


def _raw_active_content(data: bytes) -> bool:
    """A forbidden name in the text of the file, outside its streams and its
    strings. A second look, best effort: the object walk is what decides, and
    it also sees what lives in compressed object streams."""
    text = _without_strings(_without_streams(data))
    for match in _RAW_NAME.finditer(text):
        raw = _NAME_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), match.group())
        if raw.decode("latin-1") in _FORBIDDEN_NAMES:
            return True
    return False


def _without_streams(data: bytes) -> bytes:
    """`data` with every `stream ... endstream` body cut out. Linear: a file
    full of `stream` and no `endstream` costs one pass, not one per marker."""
    parts: list[bytes] = []
    position = 0
    while True:
        begin = data.find(b"stream", position)
        if begin < 0:
            break
        end = data.find(b"endstream", begin + len(b"stream"))
        if end < 0:
            break
        parts.append(data[position:begin])
        position = end + len(b"endstream")
    parts.append(data[position:])
    return b" ".join(parts)


def _without_strings(data: bytes) -> bytes:
    """`data` with every `( ... )` literal string cut out (parentheses nest and
    a backslash escapes the next byte). Text after a string that never closes
    is kept: it must not hide what follows it."""
    parts: list[bytes] = []
    last = 0
    depth = 0
    escaped = -1
    for match in _PAREN_OR_BACKSLASH.finditer(data):
        at = match.start()
        if at == escaped:
            continue
        byte = data[at]
        if byte == 0x5C:  # backslash
            if depth:
                escaped = at + 1
        elif byte == 0x28:  # (
            if depth == 0:
                parts.append(data[last:at])
            depth += 1
        elif depth:  # )
            depth -= 1
            if depth == 0:
                last = at + 1
    parts.append(data[last:])  # after the last string; all of it if one is open
    return b" ".join(parts)


def _declared_page_count(root: Any) -> int | None:
    pages = _resolve(root.get("/Pages"))
    count = (
        _resolve(pages.get("/Count")) if isinstance(pages, DictionaryObject) else None
    )
    return (
        int(count)
        if isinstance(count, int | float) and not isinstance(count, bool)
        else None
    )


def _count_pages(root: Any, max_pages: int) -> int:
    """Leaf pages of the page tree, walked with its own limits (a loop, a
    huge tree or a count that lies cannot make it run away)."""
    top = _resolve(root.get("/Pages"))
    if not isinstance(top, DictionaryObject):
        raise DocumentInvalidError("corrupt")
    seen: set[int] = set()
    stack: list[Any] = [top]
    pages = 0
    visited = 0
    while stack:
        node = _resolve(stack.pop())
        if not isinstance(node, DictionaryObject):
            raise DocumentInvalidError("corrupt")
        visited += 1
        if visited > _MAX_PAGE_TREE_NODES:
            raise DocumentInvalidError("corrupt")
        if id(node) in seen:
            raise DocumentInvalidError("corrupt")  # a loop in the tree
        seen.add(id(node))
        kids = _resolve(node.get("/Kids"))
        if node.get("/Type") == "/Pages" or isinstance(kids, ArrayObject):
            if not isinstance(kids, ArrayObject):
                raise DocumentInvalidError("corrupt")
            stack.extend(kids)
        else:
            pages += 1
            if pages > max_pages:
                raise DocumentInvalidError("too_many_pages")
    return pages


def _resolve(value: Any) -> Any:
    return value.get_object() if isinstance(value, IndirectObject) else value


def _object_numbers(reader: PdfReader) -> Iterator[tuple[int, int]]:
    """Every (number, generation) the file's cross-reference data lists,
    including the objects packed inside object streams."""
    seen: set[tuple[int, int]] = set()
    for generation, entries in reader.xref.items():
        for number in entries:
            seen.add((number, generation))
    for number in reader.xref_objStm:
        seen.add((number, 0))
    if len(seen) > MAX_OBJECTS:
        raise DocumentInvalidError("corrupt")
    yield from sorted(seen)


def _refuse_active_objects(reader: PdfReader) -> None:
    for number, generation in _object_numbers(reader):
        try:
            obj = IndirectObject(number, generation, reader).get_object()
        except Exception as exc:
            # An object a viewer might read but this parser cannot is not one
            # to wave through.
            raise DocumentInvalidError("corrupt") from exc
        _scan(obj, 0)


def _scan(value: Any, depth: int) -> None:
    """Looks through one object's own dictionaries and arrays (indirect
    references are other objects, visited on their own turn)."""
    if depth > _MAX_DEPTH:
        raise DocumentInvalidError("corrupt")
    if isinstance(value, NameObject | str) and str(value).startswith("/"):
        if _decoded(str(value)) in _FORBIDDEN_NAMES:
            raise DocumentInvalidError("active_content")
    elif isinstance(value, DictionaryObject):
        for key, item in value.items():
            if _decoded(str(key)) in _FORBIDDEN_NAMES:
                raise DocumentInvalidError("active_content")
            _scan(item, depth + 1)
    elif isinstance(value, ArrayObject):
        for item in value:
            _scan(item, depth + 1)
