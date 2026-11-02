"""Hostile bytes: whatever an upload contains, the parsers either clean it or
raise a domain error. Never another exception, never a hang."""

import io
import random

import pytest

from app.assets.artwork import find_artwork
from app.assets.audio import process_audio
from app.exceptions import AudioInvalidError, AudioTooLongError
from tests import audio_files as af

SEEDS = {
    "mp3": af.tagged_mp3(12),
    "m4a-moov-last": af.m4a(),
    "m4a-moov-first": af.m4a(moov_first=True),
    "m4a-co64": af.m4a(wide=True),
}
ROUNDS = 1500


def mutate(data: bytes, rng: random.Random) -> bytes:
    out = bytearray(data)
    for _ in range(rng.choice((1, 1, 2, 4, 8))):
        kind = rng.randrange(6)
        at = rng.randrange(len(out)) if out else 0
        if kind == 0 and out:
            out[at] = rng.randrange(256)
        elif kind == 1 and out:
            out[at] ^= 1 << rng.randrange(8)
        elif kind == 2:
            del out[at : at + rng.randrange(1, 64)]
        elif kind == 3:
            out[at:at] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 32)))
        elif kind == 4 and len(out) >= 4:
            # A length or count field set to something extreme.
            out[at : at + 4] = rng.choice(
                (0, 1, 7, 0xFFFF, 0x7FFFFFFF, 0xFFFFFFFF)
            ).to_bytes(4, "big")
        elif kind == 5:
            out = out[: rng.randrange(len(out) + 1)]
    return bytes(out)


@pytest.mark.parametrize("name", sorted(SEEDS))
def test_mutated_files_are_cleaned_or_refused_never_crash(name: str) -> None:
    rng = random.Random(name)
    accepted = 0
    for _ in range(ROUNDS):
        data = mutate(SEEDS[name], rng)
        try:
            result = process_audio(io.BytesIO(data))
        except (AudioInvalidError, AudioTooLongError):
            continue
        accepted += 1
        # Whatever is accepted must itself be a clean, acceptable file.
        again = process_audio(io.BytesIO(result.data))
        assert again.data == result.data
    assert accepted < ROUNDS  # the mutations really do break files


ART_SEEDS = {
    "id3v2.3": af.mp3_with_artwork(
        af.id3v2_with_pictures([(0, b"a" * 40), (3, b"b" * 60)])
    ),
    "id3v2.4": af.mp3_with_artwork(af.id3v2_with_pictures([(3, b"c" * 60)], version=4)),
    "id3v2.4-unsync": af.mp3_with_artwork(
        af.id3v2_with_pictures([(3, b"\xff" * 60)], version=4, unsync=True)
    ),
    "id3v2.2": af.mp3_with_artwork(af.id3v2_with_pictures([(3, b"d" * 60)], version=2)),
    "m4a": af.m4a(covers=[b"e" * 60, b"f" * 60]),
    "m4a-moov-first": af.m4a(covers=[b"g" * 60], moov_first=True),
}


@pytest.mark.parametrize("name", sorted(ART_SEEDS))
def test_looking_for_artwork_in_mutated_files_never_crashes(name: str) -> None:
    """The artwork reader is given every mutation, even of files the audio
    parser would refuse: it answers (a picture, or none) and raises nothing."""
    rng = random.Random(name)
    for _ in range(ROUNDS):
        data = mutate(ART_SEEDS[name], rng)
        found = find_artwork(io.BytesIO(data), 1000)
        # Whatever it hands back respects the cap it was given.
        if found is not None and found.data is not None:
            assert len(found.data) <= 1000


@pytest.mark.parametrize("size", [0, 1, 3, 4, 7, 8, 11, 12, 100])
def test_tiny_inputs_are_refused(size: int) -> None:
    rng = random.Random(size)
    for _ in range(50):
        data = bytes(rng.randrange(256) for _ in range(size))
        with pytest.raises(AudioInvalidError):
            process_audio(io.BytesIO(data))


def test_a_box_that_claims_a_huge_table_does_not_exhaust_memory() -> None:
    data = bytearray(af.m4a())
    at = bytes(data).index(b"stsz") + 4 + 8  # the sample count
    data[at : at + 4] = (0xFFFFFFFF).to_bytes(4, "big")

    with pytest.raises(AudioInvalidError):
        process_audio(io.BytesIO(bytes(data)))

    data = bytearray(af.m4a())
    # A uniform sample size with a huge count: sizes would be a list of 4G items.
    start = bytes(data).index(b"stsz") + 4 + 4
    data[start : start + 8] = (7).to_bytes(4, "big") + (0xFFFFFFFF).to_bytes(4, "big")
    with pytest.raises(AudioInvalidError):
        process_audio(io.BytesIO(bytes(data)))
