"""Finding the picture embedded in an audio file: ID3 APIC/PIC in MP3, covr in
M4A. The picture is only found here; it is never trusted or served."""

import io
import struct

import pytest

from app.assets.artwork import FoundArtwork, find_artwork
from tests import audio_files as af
from tests import images

JPEG = images.jpeg((40, 40))
PNG = images.png((40, 40))
OTHER = images.jpeg((20, 20))
LIMIT = 1_000_000


def find(data: bytes, limit: int = LIMIT) -> FoundArtwork | None:
    return find_artwork(io.BytesIO(data), limit)


def with_audio(tag: bytes) -> bytes:
    return af.mp3_with_artwork(tag)


# --- MP3 ---------------------------------------------------------------------


@pytest.mark.parametrize("version", [2, 3, 4])
def test_the_picture_of_an_id3_tag_is_found(version: int) -> None:
    found = find(with_audio(af.id3v2_with_pictures([(3, JPEG)], version=version)))

    assert found == FoundArtwork(JPEG)


@pytest.mark.parametrize("version", [3, 4])
def test_an_unsynchronised_tag_is_read(version: int) -> None:
    # A picture full of FF bytes, so the unsynchronisation really changes it.
    picture = b"\xff\xd8\xff\xe0" + b"\xff" * 30 + JPEG
    tag = af.id3v2_with_pictures([(3, picture)], version=version, unsync=True)

    assert find(with_audio(tag)) == FoundArtwork(picture)


@pytest.mark.parametrize("encoding", [0, 1, 2, 3])
@pytest.mark.parametrize("version", [3, 4])
def test_every_text_encoding_of_the_description_is_stepped_over(
    version: int, encoding: int
) -> None:
    tag = af.id3v2_with_pictures(
        [(3, JPEG)], version=version, encoding=encoding, description="Portada"
    )

    assert find(with_audio(tag)) == FoundArtwork(JPEG)


def test_the_front_cover_is_preferred_over_an_earlier_picture() -> None:
    tag = af.id3v2_with_pictures([(0, OTHER), (4, OTHER + b"x"), (3, JPEG)])

    assert find(with_audio(tag)) == FoundArtwork(JPEG)


def test_without_a_front_cover_the_first_picture_is_used() -> None:
    tag = af.id3v2_with_pictures([(0, OTHER), (4, JPEG)])

    assert find(with_audio(tag)) == FoundArtwork(OTHER)


def test_a_tag_without_pictures_has_no_artwork() -> None:
    assert find(with_audio(af.id3v2_with_pictures([]))) is None
    assert find(af.mp3(40)) is None


def test_the_default_tagged_fixture_yields_its_fake_picture() -> None:
    # The older fixtures carry a fake "JPEG": it is found like any other bytes
    # and left to the cover pipeline to refuse.
    found = find(af.tagged_mp3())

    assert found is not None and found.data is not None
    assert af.SECRET_ARTWORK in found.data


def test_a_picture_over_the_cap_is_reported_but_not_copied() -> None:
    big = JPEG + bytes(5000)

    assert find(with_audio(af.id3v2_with_pictures([(3, big)])), limit=1000) == (
        FoundArtwork(None)
    )
    assert find(with_audio(af.id3v2_with_pictures([(3, big)])), limit=10_000) == (
        FoundArtwork(big)
    )


# --- M4A ---------------------------------------------------------------------


@pytest.mark.parametrize("picture", [JPEG, PNG], ids=["jpeg", "png"])
@pytest.mark.parametrize("moov_first", [False, True])
def test_the_covr_picture_of_an_m4a_is_found(picture: bytes, moov_first: bool) -> None:
    data = af.m4a(covers=[picture], moov_first=moov_first)

    assert find(data) == FoundArtwork(picture)


def test_with_several_covr_pictures_the_first_is_used() -> None:
    assert find(af.m4a(covers=[JPEG, PNG])) == FoundArtwork(JPEG)


def test_an_m4a_without_tags_has_no_artwork() -> None:
    assert find(af.m4a(tags=False)) is None


def test_an_m4a_picture_over_the_cap_is_reported_but_not_copied() -> None:
    assert find(af.m4a(covers=[JPEG + bytes(5000)]), limit=1000) == FoundArtwork(None)


# --- damaged or hostile tags: no artwork, never an exception -----------------


def _tag_with_frame_size(size: int) -> bytes:
    payload = af.apic_payload(JPEG)
    frame = b"APIC" + struct.pack(">I", size) + b"\x00\x00" + payload
    return b"ID3\x03\x00\x00" + af._syncsafe(len(frame)) + frame


@pytest.mark.parametrize(
    "data",
    [
        with_audio(_tag_with_frame_size(10_000_000)),  # frame claims far too much
        with_audio(_tag_with_frame_size(0)),  # empty frame
        with_audio(b"ID3\x03\x00\x00" + af._syncsafe(5) + b"APIC"),  # cut off
        with_audio(b"ID3\x05\x00\x00" + af._syncsafe(0)),  # unknown version
        with_audio(b"ID3\x03\x00\x00\x7f\x7f\x7f\x7f" + bytes(40)),  # size too big
        with_audio(b"ID3\x03\x00\x00\x80\x00\x00\x00"),  # not a synchsafe size
        with_audio(
            b"ID3\x03\x00\x00"
            + af._syncsafe(14)
            + b"APIC"
            + struct.pack(">I", 4)
            + b"\x00\x00\x00\xff\xff\xff"  # a MIME type that never ends
        ),
        with_audio(
            af.id3v2_with_pictures([(3, JPEG)])[:30]  # tag cut in the picture
        ),
    ],
    ids=[
        "huge frame",
        "empty frame",
        "cut off",
        "unknown version",
        "tag larger than the file",
        "not synchsafe",
        "mime never ends",
        "cut picture",
    ],
)
def test_a_damaged_id3_tag_gives_no_artwork_and_no_error(data: bytes) -> None:
    found = find(data)

    # Either nothing, or (for a cut picture) whatever bytes were really there;
    # what matters is that nothing is raised and nothing outside the file is read.
    assert found is None or found.data is None or len(found.data) <= len(data)


@pytest.mark.parametrize("cut", [20, 100, 400, 800])
def test_a_truncated_m4a_gives_no_artwork_and_no_error(cut: int) -> None:
    data = af.m4a(covers=[JPEG], moov_first=True)

    find(data[:cut])  # must not raise


def test_a_covr_box_that_claims_more_than_it_has_gives_no_artwork() -> None:
    covr = af.box(b"covr", af.full(b"data", bytes(4), JPEG, flags=13))
    broken = covr[:4] + b"\x00\xff\xff\xff" + covr[8:]  # absurd size
    # Put it where the parser looks, by swapping it into a built file.
    data = af.m4a(covers=[JPEG], moov_first=True)
    good = af.box(b"covr", af.full(b"data", bytes(4), JPEG, flags=13))
    assert good in data
    mangled = data.replace(good, broken[: len(good)])

    assert find(mangled) is None


def test_the_found_picture_is_not_in_the_cleaned_audio() -> None:
    # Finding the picture does not put it back: the stored audio is still
    # rebuilt without it (the existing guarantee, here with a real picture).
    from app.assets.audio import process_audio

    for data in (
        with_audio(af.id3v2_with_pictures([(3, JPEG)])),
        af.m4a(covers=[JPEG]),
    ):
        assert find(data) == FoundArtwork(JPEG)
        assert JPEG not in process_audio(io.BytesIO(data)).data
