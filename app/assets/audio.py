"""Validation and cleaning of uploaded audio. Pure and synchronous.

Nothing about the upload is trusted: not its Content-Type, not its file name,
not its extension. The container is decided from the bytes and parsed strictly,
and what is stored is REBUILT from the audio alone, so tags, embedded artwork,
GPS atoms, XMP packets, padding and anything appended to the file (polyglots)
never reach storage:

- MP3: the run of MPEG-1/2/2.5 Layer III frames. ID3v2, ID3v1, APE and whatever
  else surrounds the frames is dropped.
- M4A (AAC in an MP4 container): `ftyp`, a `moov` rebuilt from an allow-list of
  boxes (so `udta`, `meta`, `uuid` and friends are gone, and the clock fields
  are zeroed) and an `mdat` holding only the bytes the sample tables point at.
  The `moov` goes first ("fast start"), which also lets a player start before
  the whole file arrives. Encrypted (DRM) tracks, fragmented files, tracks that
  are not AAC and files that reference data outside themselves are refused.

Both parsers walk untrusted input: every length is checked against the bytes
that are really there before it is used.
"""

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise
from typing import BinaryIO

from app.exceptions import AudioInvalidError, AudioTooLongError

MP3_MIME = "audio/mpeg"
M4A_MIME = "audio/mp4"
MAX_DURATION_SECONDS = 600.0

# How far past the ID3v2 tag the first frame is looked for, and how many frames
# in a row must chain correctly to believe a sync pattern is a real frame.
_SYNC_WINDOW = 64 * 1024
_CHAIN = 3
_MIN_FRAMES = 3
# What may follow the last frame: any tag-sized tail, or a tail small next to
# the audio (an APE tag, say). Anything else means the stream broke halfway.
_TAIL_SMALL = 1024
_TAIL_RATIO = 10


@dataclass(frozen=True, slots=True)
class ProcessedAudio:
    data: bytes
    mime: str
    duration_seconds: float


def process_audio(
    file: BinaryIO, max_duration_seconds: float = MAX_DURATION_SECONDS
) -> ProcessedAudio:
    """Raises `AudioInvalidError` (not MP3 or AAC/M4A, malformed, protected) or
    `AudioTooLongError`; otherwise returns the clean bytes to store."""
    file.seek(0)
    data = file.read()
    file.seek(0)
    if data[4:8] == b"ftyp":
        cleaned, duration = _clean_m4a(data)
        mime = M4A_MIME
    else:
        cleaned, duration = _clean_mp3(data)
        mime = MP3_MIME
    if duration <= 0:
        raise AudioInvalidError("the audio has no duration")
    if duration > max_duration_seconds:
        raise AudioTooLongError(max_duration_seconds)
    return ProcessedAudio(cleaned, mime, round(duration, 3))


def _u16(data: bytes | bytearray, pos: int) -> int:
    return int.from_bytes(data[pos : pos + 2], "big")


def _u32(data: bytes | bytearray, pos: int) -> int:
    return int.from_bytes(data[pos : pos + 4], "big")


def _u64(data: bytes | bytearray, pos: int) -> int:
    return int.from_bytes(data[pos : pos + 8], "big")


# --- MP3 ---------------------------------------------------------------------

_V1_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_V2_BITRATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
# By the two version bits: 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5.
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}


@dataclass(frozen=True, slots=True)
class _Frame:
    length: int  # bytes, header included
    samples: int
    stream: tuple[int, int]  # (version bits, sample rate): constant in a file


def _frame_at(data: bytes, pos: int) -> _Frame | None:
    """The Layer III frame whose header starts at `pos`, if it is a valid one."""
    if pos + 4 > len(data):
        return None
    b1, b2 = data[pos + 1], data[pos + 2]
    if data[pos] != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 3
    if version == 1 or ((b1 >> 1) & 3) != 1:  # reserved version / not Layer III
        return None
    bitrate_index, rate_index = b2 >> 4, (b2 >> 2) & 3
    if bitrate_index in (0, 15) or rate_index == 3:  # free-format / bad / reserved
        return None
    padding = (b2 >> 1) & 1
    rate = _SAMPLE_RATES[version][rate_index]
    if version == 3:
        bitrate = _V1_BITRATES[bitrate_index] * 1000
        return _Frame(144 * bitrate // rate + padding, 1152, (version, rate))
    bitrate = _V2_BITRATES[bitrate_index] * 1000
    return _Frame(72 * bitrate // rate + padding, 576, (version, rate))


def _skip_id3v2(data: bytes) -> int:
    pos = 0
    while data[pos : pos + 3] == b"ID3":
        if len(data) < pos + 10 or any(b & 0x80 for b in data[pos + 6 : pos + 10]):
            raise AudioInvalidError("broken ID3 header")
        size = 0
        for b in data[pos + 6 : pos + 10]:
            size = (size << 7) | b
        footer = 10 if data[pos + 5] & 0x10 else 0
        pos += 10 + size + footer
        if pos > len(data):
            raise AudioInvalidError("the ID3 tag runs past the end of the file")
    return pos


def _chains(data: bytes, pos: int) -> bool:
    """Whether `_CHAIN` frames follow each other from `pos` (fewer only when
    the file ends first, but never fewer than two)."""
    first = _frame_at(data, pos)
    if first is None:
        return False
    found, at = 0, pos
    while found < _CHAIN:
        frame = _frame_at(data, at)
        if frame is None or frame.stream != first.stream:
            return False
        at += frame.length
        found += 1
        if at > len(data):
            return False
        if at == len(data):
            return found >= 2
    return True


def _first_frame(data: bytes, start: int) -> int:
    limit = min(len(data), start + _SYNC_WINDOW)
    pos = data.find(b"\xff", start, limit)
    while pos != -1:
        if _chains(data, pos):
            return pos
        pos = data.find(b"\xff", pos + 1, limit)
    raise AudioInvalidError("not an MP3 or AAC (M4A) file")


def _clean_mp3(data: bytes) -> tuple[bytes, float]:
    start = _first_frame(data, _skip_id3v2(data))
    pos, frames, samples = start, 0, 0
    stream: tuple[int, int] | None = None
    while True:
        frame = _frame_at(data, pos)
        if frame is None or pos + frame.length > len(data):
            break
        if stream is None:
            stream = frame.stream
        elif frame.stream != stream:
            break
        pos += frame.length
        frames += 1
        samples += frame.samples
    if stream is None or frames < _MIN_FRAMES:
        raise AudioInvalidError("too few MP3 frames")
    tail = len(data) - pos
    if tail > _TAIL_SMALL and (pos - start) < _TAIL_RATIO * tail:
        raise AudioInvalidError("the MP3 stream is cut by data that is not audio")
    return data[start:pos], samples / stream[1]


# --- M4A ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Box:
    kind: bytes
    start: int  # of the header
    body: int  # first byte after the header
    end: int


def _boxes(
    data: bytes, start: int, end: int, *, last_may_extend: bool = False
) -> Iterator[_Box]:
    """The boxes laid out one after another in `data[start:end]`, which they
    must fill exactly. A box that claims more than is there is an error."""
    pos = start
    while pos < end:
        if end - pos < 8:
            raise AudioInvalidError("broken MP4 box")
        size, kind = _u32(data, pos), data[pos + 4 : pos + 8]
        header = 8
        if size == 1:
            if end - pos < 16:
                raise AudioInvalidError("broken MP4 box")
            size, header = _u64(data, pos + 8), 16
        elif size == 0:
            if not last_may_extend:
                raise AudioInvalidError("broken MP4 box")
            size = end - pos
        if size < header or pos + size > end:
            raise AudioInvalidError("an MP4 box runs past its parent")
        yield _Box(kind, pos, pos + header, pos + size)
        pos += size


def _make_box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind) + body


# Boxes kept when the `moov` is rebuilt, by parent. Everything else is dropped.
_KEEP: dict[bytes, frozenset[bytes]] = {
    b"moov": frozenset({b"mvhd", b"trak"}),
    b"trak": frozenset({b"tkhd", b"edts", b"mdia"}),
    b"mdia": frozenset({b"mdhd", b"hdlr", b"minf"}),
    b"minf": frozenset({b"smhd", b"dinf", b"stbl"}),
    b"stbl": frozenset(
        {b"stsd", b"stts", b"stsc", b"stsz", b"stco", b"co64", b"ctts", b"stss"}
    ),
}
_AAC_OBJECT_TYPES = frozenset({0x40, 0x66, 0x67, 0x68})  # MPEG-4 and MPEG-2 AAC
_PROTECTED_ENTRIES = frozenset({b"drms", b"enca", b"drmi", b"encv", b"encs"})
_TABLES = frozenset({b"stsd", b"stts", b"stsc", b"stsz"})


@dataclass(slots=True)
class _Track:
    timescale: int = 0
    handler: bytes = b""
    ticks: int = 0  # total duration, from the time-to-sample table
    stts_samples: int = 0
    sample_count: int = 0
    uniform_size: int = 0
    sample_sizes: list[int] | None = None
    chunk_runs: list[tuple[int, int]] | None = None  # (first chunk, samples each)
    chunk_offsets: list[int] | None = None
    wide_offsets: bool = False


def _zero_clock(body: bytes) -> bytes:
    """A `mvhd`/`tkhd`/`mdhd` body with creation and modification times at 0."""
    version = body[0] if body else -1
    width = 8 if version == 1 else 4
    if version not in (0, 1) or len(body) < 4 + 2 * width:
        raise AudioInvalidError("broken MP4 header box")
    return body[:4] + bytes(2 * width) + body[4 + 2 * width :]


def _read_mdhd(body: bytes, track: _Track) -> bytes:
    cleaned = _zero_clock(body)
    at = 20 if body[0] == 1 else 12
    if len(body) < at + 4:
        raise AudioInvalidError("broken MP4 header box")
    track.timescale = _u32(body, at)
    return cleaned


def _clean_hdlr(body: bytes, track: _Track) -> bytes:
    if len(body) < 24:
        raise AudioInvalidError("broken MP4 handler box")
    track.handler = body[8:12]
    # The handler's name is free text: keep the type, drop the name.
    return body[:24] + b"\x00"


def _read_descriptor(data: bytes, pos: int, end: int) -> tuple[int, int, int]:
    """(tag, payload start, payload end) of the MPEG-4 descriptor at `pos`."""
    if pos >= end:
        raise AudioInvalidError("broken audio description")
    tag, pos = data[pos], pos + 1
    size = 0
    for _ in range(4):
        if pos >= end:
            raise AudioInvalidError("broken audio description")
        b, pos = data[pos], pos + 1
        size = (size << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    if pos + size > end:
        raise AudioInvalidError("broken audio description")
    return tag, pos, pos + size


def _check_esds(body: bytes) -> None:
    """The elementary stream must be AAC and must not point at a URL."""
    tag, pos, stop = _read_descriptor(body, 4, len(body))  # past version, flags
    if tag != 0x03 or pos + 3 > stop:
        raise AudioInvalidError("broken audio description")
    flags = body[pos + 2]
    pos += 3
    if flags & 0x80:
        pos += 2
    if flags & 0x40:
        raise AudioInvalidError("the audio refers to an external stream")
    if flags & 0x20:
        pos += 2
    tag, pos, stop = _read_descriptor(body, pos, stop)
    if tag != 0x04 or pos >= stop:
        raise AudioInvalidError("broken audio description")
    if body[pos] not in _AAC_OBJECT_TYPES:
        raise AudioInvalidError("only AAC audio is accepted")


def _check_stsd(body: bytes) -> None:
    if len(body) < 8 or _u32(body, 4) != 1:
        raise AudioInvalidError("expected a single audio format")
    entries = list(_boxes(body, 8, len(body)))
    if len(entries) != 1:
        raise AudioInvalidError("expected a single audio format")
    entry = entries[0]
    if entry.kind in _PROTECTED_ENTRIES:
        raise AudioInvalidError("protected audio is not accepted")
    if entry.kind != b"mp4a":
        raise AudioInvalidError("only AAC audio is accepted")
    if entry.end - entry.body < 28 or _u16(body, entry.body + 8) != 0:
        raise AudioInvalidError("unsupported audio description")
    found_esds = False
    for child in _boxes(body, entry.body + 28, entry.end):
        if child.kind == b"sinf":
            raise AudioInvalidError("protected audio is not accepted")
        if child.kind == b"esds":
            _check_esds(body[child.body : child.end])
            found_esds = True
    if not found_esds:
        raise AudioInvalidError("the audio has no stream description")


def _check_dinf(body: bytes) -> None:
    """Every data reference must be to the file itself."""
    for child in _boxes(body, 0, len(body)):
        if child.kind != b"dref":
            continue
        if child.end - child.body < 8:
            raise AudioInvalidError("broken data reference")
        entries = list(_boxes(body, child.body + 8, child.end))
        if len(entries) != _u32(body, child.body + 4) or not entries:
            raise AudioInvalidError("broken data reference")
        for entry in entries:
            if entry.end - entry.body < 4 or not body[entry.body + 3] & 1:
                raise AudioInvalidError("the audio refers to data outside the file")


def _read_table(kind: bytes, body: bytes, track: _Track) -> None:
    """Notes what the sample tables say; other boxes are only carried over."""
    count = _u32(body, 4) if len(body) >= 8 else -1
    if kind == b"stsz":
        if len(body) < 12:
            raise AudioInvalidError("broken sample table")
        uniform, total = _u32(body, 4), _u32(body, 8)
        track.uniform_size, track.sample_count = uniform, total
        if uniform == 0:
            if len(body) < 12 + 4 * total:
                raise AudioInvalidError("broken sample table")
            track.sample_sizes = [_u32(body, 12 + 4 * i) for i in range(total)]
    elif kind == b"stts":
        if count < 1 or len(body) < 8 + 8 * count:
            raise AudioInvalidError("broken sample table")
        for i in range(count):
            samples, delta = _u32(body, 8 + 8 * i), _u32(body, 12 + 8 * i)
            track.stts_samples += samples
            track.ticks += samples * delta
    elif kind == b"stsc":
        if count < 1 or len(body) < 8 + 12 * count:
            raise AudioInvalidError("broken sample table")
        track.chunk_runs = [
            (_u32(body, 8 + 12 * i), _u32(body, 12 + 12 * i)) for i in range(count)
        ]
    elif kind in (b"stco", b"co64"):
        step = 8 if kind == b"co64" else 4
        if count < 1 or len(body) < 8 + step * count:
            raise AudioInvalidError("broken chunk table")
        read = _u64 if kind == b"co64" else _u32
        track.chunk_offsets = [read(body, 8 + step * i) for i in range(count)]
        track.wide_offsets = kind == b"co64"


def _rebuild(raw: bytes, box: _Box, track: _Track, seen: set[bytes]) -> bytes:
    """`box` rebuilt from the allow-list, noting what the track needs."""
    if box.kind in _KEEP:
        keep = _KEEP[box.kind]
        parts = [
            _rebuild(raw, child, track, seen)
            for child in _boxes(raw, box.body, box.end)
            if child.kind in keep
        ]
        return _make_box(box.kind, b"".join(parts))
    body = raw[box.body : box.end]
    if box.kind in (b"mvhd", b"tkhd"):
        body = _zero_clock(body)
    elif box.kind == b"mdhd":
        body = _read_mdhd(body, track)
    elif box.kind == b"hdlr":
        body = _clean_hdlr(body, track)
    elif box.kind == b"stsd":
        _check_stsd(body)
    elif box.kind == b"dinf":
        _check_dinf(body)
    else:
        _read_table(box.kind, body, track)
    seen.add(box.kind)
    return _make_box(box.kind, body)


def _clean_m4a(data: bytes) -> tuple[bytes, float]:
    top = list(_boxes(data, 0, len(data), last_may_extend=True))
    kinds = [b.kind for b in top]
    if not top or kinds[0] != b"ftyp" or kinds.count(b"ftyp") != 1:
        raise AudioInvalidError("not an MP4 audio file")
    if kinds.count(b"moov") != 1 or b"mdat" not in kinds:
        raise AudioInvalidError("the audio is incomplete")
    if {b"moof", b"mfra", b"sidx"} & set(kinds):
        raise AudioInvalidError("fragmented MP4 is not accepted")
    ftyp = data[top[0].start : top[0].end]
    if len(ftyp) < 16 or ftyp[8:12] == b"M4P ":
        raise AudioInvalidError("protected audio is not accepted")
    moov = next(b for b in top if b.kind == b"moov")
    children = list(_boxes(data, moov.body, moov.end))
    if any(c.kind == b"mvex" for c in children):
        raise AudioInvalidError("fragmented MP4 is not accepted")
    if sum(1 for c in children if c.kind == b"trak") != 1:
        raise AudioInvalidError("expected exactly one audio track")

    track = _Track()
    seen: set[bytes] = set()
    new_moov = _rebuild(data, moov, track, seen)
    if track.handler != b"soun" or track.timescale <= 0:
        raise AudioInvalidError("the track is not audio")
    if not _TABLES <= seen or not ({b"stco", b"co64"} & seen):
        raise AudioInvalidError("the audio has no sample tables")
    if track.stts_samples != track.sample_count:
        raise AudioInvalidError("the sample tables disagree")

    mdats = [b for b in top if b.kind == b"mdat"]
    payload, starts = _gather_chunks(data, mdats, track)
    return _assemble(ftyp, new_moov, payload, starts, track), (
        track.ticks / track.timescale
    )


def _gather_chunks(
    data: bytes, mdats: list[_Box], track: _Track
) -> tuple[bytes, list[int]]:
    """The bytes of every chunk, in order, and where each starts within them.
    Bytes of the `mdat` that no sample points at are left behind."""
    runs, old_offsets = track.chunk_runs, track.chunk_offsets
    if runs is None or old_offsets is None:
        raise AudioInvalidError("the audio has no sample tables")
    if not 0 < track.sample_count <= len(data):
        raise AudioInvalidError("the audio has no samples")
    if track.sample_sizes is not None:
        sizes = track.sample_sizes
    else:
        sizes = [track.uniform_size] * track.sample_count
    if runs[0][0] != 1 or any(a[0] >= b[0] for a, b in pairwise(runs)):
        raise AudioInvalidError("broken chunk table")
    spans = [(m.body, m.end) for m in mdats]
    pieces: list[bytes] = []
    starts: list[int] = []
    taken, run, total = 0, 0, 0
    for index, offset in enumerate(old_offsets, start=1):
        while run + 1 < len(runs) and runs[run + 1][0] <= index:
            run += 1
        count = runs[run][1]
        length = sum(sizes[taken : taken + count])
        taken += count
        inside = any(a <= offset and offset + length <= b for a, b in spans)
        if count == 0 or not inside:
            raise AudioInvalidError("a chunk lies outside the audio data")
        pieces.append(data[offset : offset + length])
        starts.append(total)
        total += length
    if taken != track.sample_count:
        raise AudioInvalidError("the chunk table does not match the samples")
    return b"".join(pieces), starts


def _chunk_table(moov: bytes) -> _Box:
    """The `stco`/`co64` box of the single track of a rebuilt `moov`."""
    box = _Box(b"moov", 0, 8, len(moov))
    for kind in (b"trak", b"mdia", b"minf", b"stbl"):
        box = next(c for c in _boxes(moov, box.body, box.end) if c.kind == kind)
    return next(
        c for c in _boxes(moov, box.body, box.end) if c.kind in (b"stco", b"co64")
    )


def _assemble(
    ftyp: bytes, moov: bytes, payload: bytes, starts: list[int], track: _Track
) -> bytes:
    """ftyp + moov + mdat, with the chunk offsets pointed at the new mdat."""
    if 8 + len(payload) >= 1 << 32:
        raise AudioInvalidError("the audio is too large")
    base = len(ftyp) + len(moov) + 8  # where the first chunk will be
    step = 8 if track.wide_offsets else 4
    table = _chunk_table(moov)
    patched = bytearray(moov)
    for i, start in enumerate(starts):
        at = table.body + 8 + step * i
        patched[at : at + step] = (base + start).to_bytes(step, "big")
    return ftyp + bytes(patched) + _make_box(b"mdat", payload)
