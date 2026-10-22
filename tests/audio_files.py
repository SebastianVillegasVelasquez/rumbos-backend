"""Builders for test audio: structurally valid MP3 and M4A files (the payload is
silence or filler, nobody decodes it), with tags that must not survive, and
hostile variants. No encoder is needed."""

import struct

SECRET_TITLE = b"SecretTitle-4711"
SECRET_ARTWORK = b"SecretArtwork-0815"
SECRET_GPS = b"+04.6097-074.0817/"
SECRET_XMP = b"SecretXmp-1234"
SECRET_FREE = b"SecretFree-5678"
SECRET_HANDLER = b"Alice's recorder"
SECRET_TAILS = (b"SecretId3v1-1111", b"SecretApe-2222")
ALL_SECRETS = (
    SECRET_TITLE,
    SECRET_ARTWORK,
    SECRET_GPS,
    SECRET_XMP,
    SECRET_FREE,
    SECRET_HANDLER,
    *SECRET_TAILS,
)

# --- MP3 -----------------------------------------------------------------------

# MPEG-1 Layer III, 128 kbps, 44.1 kHz: 417 bytes, 1152 samples.
FRAME_44K = b"\xff\xfb\x90\x00" + bytes(413)
FRAME_44K_SECONDS = 1152 / 44100
# MPEG-2.5 Layer III, 8 kbps, 8 kHz: 72 bytes, 576 samples (cheap long audio).
FRAME_8K = b"\xff\xe3\x18\xc0" + bytes(68)
FRAME_8K_SECONDS = 576 / 8000
# MPEG-1 Layer III, 128 kbps, 48 kHz: 384 bytes.
FRAME_48K = b"\xff\xfb\x94\x00" + bytes(380)


def mp3(frames: int = 40, frame: bytes = FRAME_44K) -> bytes:
    """Just the audio: `frames` frames."""
    return frame * frames


def id3v2(*, size_padding: int = 0) -> bytes:
    """An ID3v2.3 tag with a title and a (fake) cover picture."""
    title = (
        b"TIT2"
        + struct.pack(">I", len(SECRET_TITLE) + 1)
        + b"\x00\x00\x00"
        + SECRET_TITLE
    )
    picture = b"\x00image/jpeg\x00\x03\x00" + b"\xff\xd8\xff\xe0" + SECRET_ARTWORK
    apic = b"APIC" + struct.pack(">I", len(picture)) + b"\x00\x00" + picture
    body = title + apic + bytes(size_padding)
    size = bytes(((len(body) >> s) & 0x7F) for s in (21, 14, 7, 0))
    return b"ID3\x03\x00\x00" + size + body


def id3v1() -> bytes:
    return (b"TAG" + SECRET_TAILS[0]).ljust(128, b"\x00")


def ape_tag() -> bytes:
    footer = b"APETAGEX" + struct.pack("<IIII", 2000, 32 + len(SECRET_TAILS[1]), 1, 0)
    return SECRET_TAILS[1] + footer + bytes(8)


def tagged_mp3(frames: int = 40, frame: bytes = FRAME_44K) -> bytes:
    return id3v2(size_padding=64) + mp3(frames, frame) + ape_tag() + id3v1()


# --- M4A -----------------------------------------------------------------------


def box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def full(kind: bytes, *parts: bytes, version: int = 0, flags: int = 0) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big"), *parts)


def descriptor(tag: int, payload: bytes) -> bytes:
    return bytes([tag, len(payload)]) + payload


def esds(object_type: int = 0x40, *, url: bool = False) -> bytes:
    config = descriptor(
        4,
        bytes([object_type, 0x15]) + bytes(3) + bytes(8) + descriptor(5, b"\x12\x10"),
    )
    es = descriptor(3, struct.pack(">HB", 0, 0x40 if url else 0) + config)
    return full(b"esds", es)


CLOCK = 3_700_000_000  # a non-zero creation time, to see it zeroed

SAMPLE_COUNT = 45
SAMPLE_SIZE = 100
SAMPLES_PER_CHUNK = 5
TIMESCALE = 44100
SAMPLE_DELTA = 1024
M4A_SECONDS = SAMPLE_COUNT * SAMPLE_DELTA / TIMESCALE


def sample(i: int) -> bytes:
    return bytes([i + 1]) * SAMPLE_SIZE


def sample_data() -> bytes:
    return b"".join(sample(i) for i in range(SAMPLE_COUNT))


def _mvhd() -> bytes:
    return full(
        b"mvhd",
        struct.pack(">IIII", CLOCK, CLOCK, 1000, 1000),
        struct.pack(">IH", 0x10000, 0x100),
        bytes(10),
        bytes(36),
        bytes(24),
        struct.pack(">I", 2),
    )


def _tkhd() -> bytes:
    return full(
        b"tkhd",
        struct.pack(">IIIII", CLOCK, CLOCK, 1, 0, 1000),
        bytes(8),
        bytes(8),
        bytes(36),
        bytes(8),
        flags=7,
    )


def _mdhd() -> bytes:
    return full(
        b"mdhd",
        struct.pack(">IIII", CLOCK, CLOCK, TIMESCALE, SAMPLE_COUNT * SAMPLE_DELTA),
        struct.pack(">HH", 0x55C4, 0),
    )


def _hdlr(handler: bytes, name: bytes) -> bytes:
    return full(b"hdlr", bytes(4), handler, bytes(12), name + b"\x00")


def _dinf(*, external: bool = False) -> bytes:
    url = full(b"url ", flags=0 if external else 1)
    return box(b"dinf", full(b"dref", struct.pack(">I", 1), url))


def _stsd(
    entry: bytes = b"mp4a",
    object_type: int = 0x40,
    *,
    drm: bool = False,
    url: bool = False,
) -> bytes:
    children = esds(object_type, url=url)
    if drm:
        children += box(b"sinf", full(b"schm", b"itun", bytes(4)))
    audio = (
        bytes(6)
        + struct.pack(">H", 1)
        + bytes(8)
        + struct.pack(">HHHH", 2, 16, 0, 0)
        + struct.pack(">I", TIMESCALE << 16)
    )
    return full(b"stsd", struct.pack(">I", 1), box(entry, audio, children))


def _tables(offsets: list[int], *, wide: bool = False) -> bytes:
    sizes = b"".join(struct.pack(">I", SAMPLE_SIZE) for _ in range(SAMPLE_COUNT))
    chunks = b"".join(struct.pack(">Q" if wide else ">I", o) for o in offsets)
    return b"".join(
        (
            full(b"stts", struct.pack(">III", 1, SAMPLE_COUNT, SAMPLE_DELTA)),
            full(b"stsc", struct.pack(">IIII", 1, 1, SAMPLES_PER_CHUNK, 1)),
            full(b"stsz", struct.pack(">II", 0, SAMPLE_COUNT), sizes),
            full(b"co64" if wide else b"stco", struct.pack(">I", len(offsets)), chunks),
        )
    )


def _tags_in_udta() -> bytes:
    name = box(b"\xa9nam", full(b"data", bytes(4), SECRET_TITLE, flags=1))
    cover = box(
        b"covr", full(b"data", bytes(4), b"\xff\xd8\xff" + SECRET_ARTWORK, flags=13)
    )
    ilst = box(b"ilst", name, cover)
    meta = full(b"meta", _hdlr(b"mdir", b""), ilst)
    return box(b"udta", meta, box(b"\xa9xyz", SECRET_GPS))


def moov(
    offsets: list[int],
    *,
    tags: bool = True,
    handler: bytes = b"soun",
    entry: bytes = b"mp4a",
    object_type: int = 0x40,
    drm: bool = False,
    external: bool = False,
    url: bool = False,
    wide: bool = False,
    tracks: int = 1,
    fragmented: bool = False,
) -> bytes:
    stbl = box(
        b"stbl",
        _stsd(entry, object_type, drm=drm, url=url),
        _tables(offsets, wide=wide),
    )
    minf = box(b"minf", full(b"smhd", bytes(4)), _dinf(external=external), stbl)
    mdia = box(b"mdia", _mdhd(), _hdlr(handler, SECRET_HANDLER), minf)
    trak = box(b"trak", _tkhd(), mdia, *([_tags_in_udta()] if tags else []))
    return box(
        b"moov",
        _mvhd(),
        *([trak] * tracks),
        *([box(b"mvex", full(b"trex", bytes(20)))] if fragmented else []),
        *([_tags_in_udta()] if tags else []),
    )


def ftyp(major: bytes = b"M4A ") -> bytes:
    return box(b"ftyp", major, bytes(4), b"M4A ", b"mp42", b"isom")


def _chunks() -> list[bytes]:
    data = [sample(i) for i in range(SAMPLE_COUNT)]
    return [
        b"".join(data[i : i + SAMPLES_PER_CHUNK])
        for i in range(0, SAMPLE_COUNT, SAMPLES_PER_CHUNK)
    ]


def m4a(
    *,
    moov_first: bool = False,
    tags: bool = True,
    junk: bool = True,
    brand: bytes = b"M4A ",
    skew: int = 0,
    **options: object,
) -> bytes:
    """A valid one-track AAC file. `moov_first` puts the index before the data;
    `junk` adds unreferenced bytes inside the mdat and metadata boxes around it;
    `skew` points every chunk offset that many bytes off."""
    chunks = _chunks()
    payload = (SECRET_FREE if junk else b"") + b"".join(chunks)
    mdat = box(b"mdat", payload)
    extras = (
        [box(b"free", SECRET_FREE), box(b"uuid", bytes(16), SECRET_XMP)] if junk else []
    )
    head = ftyp(brand) + b"".join(extras)
    wide = bool(options.get("wide"))

    def offsets(base: int) -> list[int]:
        out, at = [], base + 8 + (len(SECRET_FREE) if junk else 0)
        for chunk in chunks:
            out.append(at + skew)
            at += len(chunk)
        return out

    def built(base: int) -> bytes:
        return moov(offsets(base), tags=tags, **options)  # type: ignore[arg-type]

    if moov_first:
        size = len(built(0))
        return head + built(len(head) + size) + mdat
    del wide
    return head + mdat + built(len(head))
