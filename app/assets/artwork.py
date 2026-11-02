"""Finds the picture embedded in an uploaded audio file. Pure and synchronous.

The picture is only FOUND here, as raw bytes, never trusted and never served:
it goes through the cover pipeline (`app.assets.processing.process_cover`),
which decodes it and writes a new image. The audio itself is still rebuilt
without it (see `app.assets.audio`).

- MP3: the `APIC` frame (`PIC` in ID3v2.2) of the ID3v2 tag at the start of the
  file. The front cover is preferred; otherwise the first picture.
- M4A: the first `data` box of `moov/udta/meta/ilst/covr`.

Both walk untrusted input: every length is checked against the bytes that are
really there. A file whose artwork cannot be read simply has none: the caller
never fails the audio because of it. A picture over `max_bytes` is reported as
found but not copied (`FoundArtwork.data` is None), so a hostile tag cannot
make the app hold a huge picture.
"""

from dataclasses import dataclass
from typing import BinaryIO

# The same bounded box walker that rebuilds the M4A: one reader of untrusted
# MP4 structure, not two.
from app.assets.audio import _Box, _boxes
from app.exceptions import AudioInvalidError

_FRONT_COVER = 3


@dataclass(frozen=True, slots=True)
class FoundArtwork:
    # None: there is a picture but it is over the byte cap, so it was not copied.
    data: bytes | None


def find_artwork(file: BinaryIO, max_bytes: int) -> FoundArtwork | None:
    """The picture embedded in an audio file (already accepted by
    `process_audio`), or None when there is none or it cannot be read."""
    file.seek(0)
    data = file.read()
    file.seek(0)
    try:
        if data[4:8] == b"ftyp":
            return _mp4(data, max_bytes)
        return _id3(data, max_bytes)
    except (AudioInvalidError, ValueError, IndexError):
        return None  # damaged tag: no artwork, the audio is judged on its own


# --- MP4 ---------------------------------------------------------------------


def _child(data: bytes, parent: _Box, kind: bytes, skip: int = 0) -> _Box | None:
    """The first child box of `kind`, with `skip` bytes before the children."""
    children = _boxes(data, parent.body + skip, parent.end, last_may_extend=True)
    return next((c for c in children if c.kind == kind), None)


def _mp4(data: bytes, max_bytes: int) -> FoundArtwork | None:
    # The file as one box, so the top level is walked like any other.
    box: _Box | None = _Box(b"", 0, 0, len(data))
    # `meta` is a full box: four bytes of version and flags before its children,
    # so that is how far into it the search for `ilst` starts.
    for kind, skip in (
        (b"moov", 0),
        (b"udta", 0),
        (b"meta", 0),
        (b"ilst", 4),
        (b"covr", 0),
    ):
        if box is None:
            return None
        box = _child(data, box, kind, skip)
    if box is None:
        return None
    picture = _child(data, box, b"data")
    if picture is None or picture.end - picture.body < 8:
        return None
    # Type indicator (4) and locale (4), then the picture.
    start = picture.body + 8
    if picture.end - start > max_bytes:
        return FoundArtwork(None)
    return FoundArtwork(data[start : picture.end]) if picture.end > start else None


# --- MP3 ---------------------------------------------------------------------


def _syncsafe(raw: bytes) -> int:
    if any(b & 0x80 for b in raw):
        raise ValueError("not a synchsafe integer")
    size = 0
    for b in raw:
        size = (size << 7) | b
    return size


def _unsynchronise(raw: bytes) -> bytes:
    """Undoes the unsynchronisation of a tag: `FF 00` stands for `FF`."""
    return raw.replace(b"\xff\x00", b"\xff")


def _id3(data: bytes, max_bytes: int) -> FoundArtwork | None:
    if data[:3] != b"ID3" or len(data) < 10:
        return None
    version, flags = data[3], data[5]
    if version not in (2, 3, 4):
        return None
    size = _syncsafe(data[6:10])
    if 10 + size > len(data):
        return None
    body = data[10 : 10 + size]
    if flags & 0x80 and version < 4:  # whole-tag unsynchronisation
        body = _unsynchronise(body)
    if version == 2:
        if flags & 0x40:  # compressed tag: not defined, not read
            return None
        return _pick(_frames_v22(body), max_bytes)
    if flags & 0x40:  # an extended header to step over
        raw = body[:4]
        skip = _syncsafe(raw) if version == 4 else 4 + int.from_bytes(raw, "big")
        body = body[skip:]
    return _pick(
        _frames_v23_v24(body, version, tag_unsync=bool(flags & 0x80)), max_bytes
    )


def _frames_v22(body: bytes) -> list[tuple[int, bytes]]:
    pictures: list[tuple[int, bytes]] = []
    pos = 0
    while pos + 6 <= len(body) and body[pos] != 0:
        frame_id = body[pos : pos + 3]
        size = int.from_bytes(body[pos + 3 : pos + 6], "big")
        end = pos + 6 + size
        if end > len(body):
            break
        if frame_id == b"PIC":
            picture = _picture(body[pos + 6 : end], v22=True)
            if picture is not None:
                pictures.append(picture)
        pos = end
    return pictures


def _frames_v23_v24(
    body: bytes, version: int, *, tag_unsync: bool
) -> list[tuple[int, bytes]]:
    pictures: list[tuple[int, bytes]] = []
    pos = 0
    while pos + 10 <= len(body) and body[pos] != 0:
        frame_id = body[pos : pos + 4]
        size_raw = body[pos + 4 : pos + 8]
        size = _syncsafe(size_raw) if version == 4 else int.from_bytes(size_raw, "big")
        fmt = body[pos + 9]  # the format flags (the status flags are not used)
        end = pos + 10 + size
        if end > len(body):
            break
        if frame_id == b"APIC":
            payload = body[pos + 10 : end]
            usable = True
            if version == 3:
                # compression (0x80) and encryption (0x40) cannot be read;
                # grouping (0x20) puts one byte in front.
                usable = not fmt & 0xC0
                if usable and fmt & 0x20:
                    payload = payload[1:]
            else:
                usable = not fmt & 0x0C  # compressed or encrypted
                if usable and fmt & 0x40:  # grouping identity
                    payload = payload[1:]
                if usable and fmt & 0x01:  # data length indicator
                    payload = payload[4:]
                if usable and (fmt & 0x02 or tag_unsync):
                    payload = _unsynchronise(payload)
            picture = _picture(payload, v22=False) if usable else None
            if picture is not None:
                pictures.append(picture)
        pos = end
    return pictures


def _picture(payload: bytes, *, v22: bool) -> tuple[int, bytes] | None:
    """(picture type, image bytes) of an `APIC`/`PIC` payload."""
    if len(payload) < 4:
        return None
    encoding = payload[0]
    if encoding > 3:
        return None
    pos = 1
    if v22:
        pos += 3  # a three-letter image format, no MIME type
    else:
        stop = payload.find(b"\x00", pos)
        if stop == -1:
            return None
        pos = stop + 1  # past the MIME type
    if pos >= len(payload):
        return None
    picture_type = payload[pos]
    pos += 1
    image_at = _after_text(payload, pos, encoding)
    if image_at is None or image_at >= len(payload):
        return None
    return picture_type, payload[image_at:]


def _after_text(payload: bytes, pos: int, encoding: int) -> int | None:
    """Where the text that starts at `pos` ends (past its terminator)."""
    if encoding in (0, 3):  # Latin-1, UTF-8: a single zero byte
        stop = payload.find(b"\x00", pos)
        return None if stop == -1 else stop + 1
    # UTF-16: two zero bytes on a two-byte boundary.
    while pos + 1 < len(payload):
        if payload[pos] == 0 and payload[pos + 1] == 0:
            return pos + 2
        pos += 2
    return None


def _pick(pictures: list[tuple[int, bytes]], max_bytes: int) -> FoundArtwork | None:
    if not pictures:
        return None
    # The front cover when there is one, else the first picture.
    chosen = next((p for p in pictures if p[0] == _FRONT_COVER), pictures[0])
    if len(chosen[1]) > max_bytes:
        return FoundArtwork(None)
    return FoundArtwork(chosen[1])
