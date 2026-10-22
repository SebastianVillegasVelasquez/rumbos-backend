"""Audio validation and cleaning: formats are decided from the bytes, tags and
artwork never reach storage, and malformed or protected files are refused."""

import io
import struct
from typing import Any

import pytest
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from app.assets.audio import M4A_MIME, MP3_MIME, process_audio
from app.exceptions import AudioInvalidError, AudioTooLongError
from tests import audio_files as af
from tests import images


def run(data: bytes, **kwargs: float):  # type: ignore[no-untyped-def]
    return process_audio(io.BytesIO(data), **kwargs)


# --- MP3 -----------------------------------------------------------------------


def test_an_mp3_is_accepted_and_measured() -> None:
    result = run(af.mp3(40))

    assert result.mime == MP3_MIME
    assert result.data == af.mp3(40)
    assert result.duration_seconds == pytest.approx(40 * af.FRAME_44K_SECONDS, abs=1e-3)


def test_tags_artwork_and_trailers_are_cut_off_an_mp3() -> None:
    dirty = af.tagged_mp3(40)
    assert af.SECRET_TITLE in dirty and af.SECRET_ARTWORK in dirty

    result = run(dirty)

    assert result.data == af.mp3(40)
    for secret in af.ALL_SECRETS:
        assert secret not in result.data
    # An independent parser sees no tags and the same length.
    parsed: Any = MP3(io.BytesIO(result.data))  # type: ignore[no-untyped-call]
    assert parsed.tags is None
    assert parsed.info.length == pytest.approx(result.duration_seconds, abs=0.05)


def test_an_mp3_whose_first_frame_is_not_at_the_start_is_found() -> None:
    result = run(b"\x00" * 300 + af.mp3(40))

    assert result.data == af.mp3(40)


def test_an_mpeg_2_5_mp3_is_measured_with_its_own_frame_size() -> None:
    result = run(af.mp3(200, af.FRAME_8K))

    assert result.duration_seconds == pytest.approx(200 * af.FRAME_8K_SECONDS)


def test_a_cut_last_frame_is_dropped() -> None:
    result = run(af.mp3(40) + af.FRAME_44K[:200])

    assert result.data == af.mp3(40)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello world, this is not audio" * 20,
        images.png(),
        b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(40),  # WAV
        b"OggS" + bytes(60),
        b"\xff\xf1\x50\x80" + bytes(300),  # a raw AAC (ADTS) stream
        af.mp3(2),  # fewer than three frames
        af.id3v2() + b"not frames at all" * 50,
        af.mp3(10) + bytes(range(256)) * 40 + af.mp3(30),  # cut by garbage
        af.mp3(10) + af.mp3(30, af.FRAME_48K),  # the stream changes halfway
    ],
    ids=[
        "empty",
        "text",
        "png",
        "wav",
        "ogg",
        "adts",
        "too-few-frames",
        "tag-only",
        "garbage-in-the-middle",
        "stream-changes",
    ],
)
def test_what_is_not_a_well_formed_mp3_or_m4a_is_refused(data: bytes) -> None:
    with pytest.raises(AudioInvalidError):
        run(data)


def test_a_truncated_id3_tag_is_refused() -> None:
    with pytest.raises(AudioInvalidError):
        run(af.id3v2()[:20])


def test_the_content_decides_not_the_name_or_the_claimed_type() -> None:
    # The processor never sees a file name or a Content-Type; a PNG that
    # someone calls "song.mp3" is judged by its bytes.
    with pytest.raises(AudioInvalidError):
        run(images.png())


def test_a_track_over_the_limit_is_too_long_and_one_at_it_is_not() -> None:
    frames = 100
    seconds = round(frames * af.FRAME_8K_SECONDS, 3)
    data = af.mp3(frames, af.FRAME_8K)

    assert run(data, max_duration_seconds=seconds).duration_seconds == seconds
    with pytest.raises(AudioTooLongError):
        run(data, max_duration_seconds=seconds - 0.1)


# --- M4A -----------------------------------------------------------------------


@pytest.mark.parametrize("moov_first", [False, True])
def test_an_m4a_is_accepted_and_measured(moov_first: bool) -> None:
    result = run(af.m4a(moov_first=moov_first))

    assert result.mime == M4A_MIME
    assert result.duration_seconds == pytest.approx(af.M4A_SECONDS, abs=1e-3)


def test_the_cleaned_m4a_keeps_exactly_the_samples_and_puts_the_index_first() -> None:
    result = run(af.m4a())

    kinds = _top_level(result.data)
    assert kinds == [b"ftyp", b"moov", b"mdat"]
    assert result.data.endswith(af.sample_data())
    assert struct.unpack(">I", result.data[-len(af.sample_data()) - 8 :][:4])[0] == (
        8 + len(af.sample_data())
    )


def test_metadata_artwork_gps_xmp_and_padding_are_removed_from_an_m4a() -> None:
    dirty = af.m4a()
    for secret in af.ALL_SECRETS:
        if secret not in af.SECRET_TAILS:
            assert secret in dirty

    result = run(dirty)

    for secret in af.ALL_SECRETS:
        assert secret not in result.data
    assert b"udta" not in result.data and b"ilst" not in result.data
    assert b"covr" not in result.data and b"uuid" not in result.data
    assert b"free" not in result.data


def test_the_clock_fields_are_zeroed() -> None:
    result = run(af.m4a())

    assert struct.pack(">I", af.CLOCK) not in result.data


def test_an_independent_parser_reads_the_cleaned_m4a() -> None:
    for moov_first in (False, True):
        result = run(af.m4a(moov_first=moov_first))
        parsed: Any = MP4(io.BytesIO(result.data))  # type: ignore[no-untyped-call]

        assert not parsed.tags
        assert parsed.info.codec.startswith("mp4a.40")
        assert parsed.info.length == pytest.approx(result.duration_seconds, abs=0.01)
        assert parsed.info.channels == 2


def test_chunk_offsets_point_at_the_new_data() -> None:
    result = run(af.m4a())

    offsets = _chunk_offsets(result.data)
    chunk = af.SAMPLES_PER_CHUNK * af.SAMPLE_SIZE
    for i, offset in enumerate(offsets):
        expected = af.sample_data()[i * chunk : (i + 1) * chunk]
        assert result.data[offset : offset + len(expected)] == expected


def test_a_file_with_64_bit_chunk_offsets_is_accepted() -> None:
    result = run(af.m4a(wide=True))

    assert result.data.endswith(af.sample_data())
    assert b"co64" in result.data


def test_an_m4a_with_a_sixty_four_bit_box_size_is_accepted() -> None:
    plain = af.m4a(junk=False)
    mdat_at = plain.index(b"mdat") - 4
    size = struct.unpack(">I", plain[mdat_at : mdat_at + 4])[0]
    wide = (
        plain[:mdat_at]
        + struct.pack(">I4sQ", 1, b"mdat", size + 8)
        + plain[mdat_at + 8 :]
    )
    # The offsets in the moov are 8 bytes short now.
    shifted = af.m4a(junk=False, skew=8)
    wide = (
        shifted[:mdat_at]
        + struct.pack(">I4sQ", 1, b"mdat", size + 8)
        + shifted[mdat_at + 8 :]
    )

    assert run(wide).data.endswith(af.sample_data())


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (af.m4a(drm=True), "sinf"),
        (af.m4a(entry=b"enca"), "protected entry"),
        (af.m4a(brand=b"M4P "), "FairPlay brand"),
        (af.m4a(entry=b"alac"), "ALAC"),
        (af.m4a(object_type=0x6B), "MP3 inside MP4"),
        (af.m4a(object_type=0x21), "H.264 object type"),
        (af.m4a(url=True), "external stream"),
        (af.m4a(external=True), "external data reference"),
        (af.m4a(handler=b"vide"), "video track"),
        (af.m4a(tracks=2), "two tracks"),
        (af.m4a(fragmented=True), "fragmented"),
        (af.m4a(skew=-100), "chunk before the data"),
        (af.m4a(skew=100000), "chunk after the data"),
        (af.m4a()[:-300], "truncated"),
        (af.m4a() + b"\x00" * 5, "trailing bytes"),
        (af.ftyp() + af.box(b"mdat", b"x" * 100), "no moov"),
        (af.ftyp(), "no mdat"),
        (af.box(b"free", bytes(20)) + af.m4a(), "ftyp not first"),
        (b"\x00\x00\x00\x20ftypM4A " + bytes(20), "ftyp only"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_protected_or_malformed_m4a_is_refused(data: bytes, why: str) -> None:
    with pytest.raises(AudioInvalidError):
        run(data)


def test_an_m4a_over_the_limit_is_too_long() -> None:
    with pytest.raises(AudioTooLongError):
        run(af.m4a(), max_duration_seconds=0.5)


# --- helpers -------------------------------------------------------------------


def _top_level(data: bytes) -> list[bytes]:
    kinds, pos = [], 0
    while pos < len(data):
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        kinds.append(kind)
        pos += size
    return kinds


def _chunk_offsets(data: bytes) -> list[int]:
    at = data.index(b"stco") + 4
    count = struct.unpack(">I", data[at + 4 : at + 8])[0]
    return list(struct.unpack(f">{count}I", data[at + 8 : at + 8 + 4 * count]))
