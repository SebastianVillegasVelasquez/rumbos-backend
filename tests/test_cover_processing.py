"""The cover pipeline: decode, verify, centre-crop to a square, shrink, strip
every piece of metadata, re-encode as WebP."""

import io

import pytest
from PIL import Image

from app.assets.processing import COVER_SIDE, ImageLimits, ProcessedCover, process_cover
from app.exceptions import CoverInvalidError
from tests import images

LIMITS = ImageLimits(background_side=2000, bubble_side=100, cover_side=2048)


def run(data: bytes, limits: ImageLimits = LIMITS) -> ProcessedCover:
    return process_cover(io.BytesIO(data), limits)


def reopen(cover: ProcessedCover) -> Image.Image:
    image = Image.open(io.BytesIO(cover.data))
    image.load()
    return image


def pixel(image: Image.Image, point: tuple[int, int]) -> tuple[int, ...]:
    value = image.getpixel(point)
    assert isinstance(value, tuple)
    return value


def riff_chunks(data: bytes) -> set[bytes]:
    """The four-character codes of the chunks of a WebP file."""
    chunks, pos = set(), 12
    while pos + 8 <= len(data):
        chunks.add(data[pos : pos + 4])
        pos += (
            8
            + int.from_bytes(data[pos + 4 : pos + 8], "little")
            + (int.from_bytes(data[pos + 4 : pos + 8], "little") & 1)
        )
    return chunks


def _noisy_png() -> bytes:
    """A PNG whose pixel data does not compress away, so cutting it in half
    really cuts into the pixels."""
    pixels = bytes((i * 7 + i // 64 * 13) % 256 for i in range(64 * 64 * 3))
    return images.encode(Image.frombytes("RGB", (64, 64), pixels), "PNG")


@pytest.mark.parametrize(
    "make", [images.png, images.jpeg, images.webp], ids=["png", "jpeg", "webp"]
)
def test_every_accepted_format_comes_out_as_webp(make) -> None:  # type: ignore[no-untyped-def]
    cover = run(make((64, 64)))

    assert cover.data[:4] == b"RIFF" and cover.data[8:12] == b"WEBP"
    assert reopen(cover).format == "WEBP"
    assert (cover.width, cover.height) == (64, 64)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1000, 600), COVER_SIDE),  # wide: cropped, then shrunk
        ((600, 1000), COVER_SIDE),  # tall
        ((2048, 2048), COVER_SIDE),  # the largest accepted
        ((512, 512), 512),  # exactly the cover size: kept
        ((300, 200), 200),  # smaller than the cover size: never scaled up
        ((90, 400), 90),
    ],
)
def test_the_result_is_a_square_of_at_most_512(
    size: tuple[int, int], expected: int
) -> None:
    cover = run(images.png(size))

    assert (cover.width, cover.height) == (expected, expected)
    assert reopen(cover).size == (expected, expected)


def test_a_non_square_image_is_cropped_from_its_centre() -> None:
    # Three vertical bands of 100 x 100: only the middle one (green) survives a
    # centre crop of a 300 x 100 image.
    source = Image.new("RGB", (300, 100))
    for band, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
        source.paste(color, (band * 100, 0, band * 100 + 100, 100))
    cover = run(images.encode(source, "PNG"))

    pixels = reopen(cover).convert("RGB")
    assert pixels.size == (100, 100)
    for point in [(5, 5), (50, 50), (94, 94)]:
        r, g, b = pixel(pixels, point)
        assert g > 200 and r < 60 and b < 60


def test_a_tall_image_is_cropped_from_its_centre_too() -> None:
    source = Image.new("RGB", (100, 300))
    for band, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
        source.paste(color, (0, band * 100, 100, band * 100 + 100))

    pixels = reopen(run(images.encode(source, "PNG"))).convert("RGB")

    r, g, b = pixel(pixels, (50, 50))
    assert g > 200 and r < 60 and b < 60


def test_transparency_is_kept() -> None:
    cover = run(images.png((80, 80), mode="RGBA", color=(10, 200, 30, 0)))

    image = reopen(cover)
    assert image.mode == "RGBA"
    assert pixel(image, (40, 40))[3] == 0


def test_an_opaque_image_has_no_alpha_channel() -> None:
    assert reopen(run(images.jpeg((80, 80)))).mode == "RGB"


def test_a_palette_png_with_a_transparent_index_stays_transparent() -> None:
    source = Image.new("P", (40, 40), 0)
    source.putpalette([255, 0, 0] * 256)
    cover = run(images.encode(source, "PNG", transparency=0))

    assert pixel(reopen(cover).convert("RGBA"), (20, 20))[3] == 0


def test_the_exif_orientation_is_applied_before_it_is_dropped() -> None:
    # 100 x 50 with orientation 6 (rotate 90 degrees): a 50 x 100 picture whose
    # top half is red. Centre-cropped it is a 50 x 50 square.
    source = Image.new("RGB", (100, 50), (255, 0, 0))
    source.paste((0, 0, 255), (50, 0, 100, 50))
    data = images.encode(source, "JPEG", exif=images.exif_with_gps(6).tobytes())

    cover = run(data)

    assert (cover.width, cover.height) == (50, 50)


@pytest.mark.parametrize(
    "make",
    [images.jpeg_with_metadata, images.png_with_metadata, images.webp_with_metadata],
    ids=["jpeg", "png", "webp"],
)
def test_no_metadata_survives(make) -> None:  # type: ignore[no-untyped-def]
    data = make()
    secrets = [images.SECRET_CAMERA.encode(), images.SECRET_COMMENT.encode()]
    assert any(secret in data for secret in secrets)  # the input really has some

    stored = run(data).data

    for secret in secrets:
        assert secret not in stored
    assert not {b"EXIF", b"XMP ", b"ICCP"} & riff_chunks(stored)
    reloaded = Image.open(io.BytesIO(stored))
    assert dict(reloaded.getexif()) == {}
    assert not {"exif", "xmp", "comment", "icc_profile"} & set(reloaded.info)


def test_trailing_data_does_not_survive() -> None:
    payload = b"<script>evil()</script>"

    stored = run(images.with_trailing_payload(images.png((64, 64)), payload)).data

    assert payload not in stored


def test_the_stored_bytes_depend_on_the_pixels_alone() -> None:
    # Re-encoded, never passed through: the same picture wrapped in extra bytes
    # comes out identical.
    source = images.webp((64, 64))

    assert run(images.with_trailing_payload(source)).data == run(source).data


@pytest.mark.parametrize("side", [2049, 4000])
def test_an_image_over_the_side_limit_is_refused(side: int) -> None:
    with pytest.raises(CoverInvalidError):
        run(images.png((side, 8)))


@pytest.mark.parametrize("size", [(60000, 60000), (9000, 9000), (3000, 3000)])
def test_decompression_bombs_are_refused_without_decoding(
    size: tuple[int, int],
) -> None:
    """Only a header: refused from the size alone (and nothing is allocated)."""
    with pytest.raises(CoverInvalidError):
        run(images.png_header_only(*size))


@pytest.mark.parametrize(
    "data",
    [images.animated_webp(), images.animated_png()],
    ids=["animated webp", "animated png"],
)
def test_animated_images_are_refused(data: bytes) -> None:
    with pytest.raises(CoverInvalidError):
        run(data)


@pytest.mark.parametrize(
    "data",
    [
        images.svg(),
        images.gif(),
        b"",
        b"not an image at all",
        images.png()[:30],  # truncated inside the header
        _noisy_png()[: len(_noisy_png()) // 2],  # truncated inside the pixel data
        images.jpeg()[:60],
        b"RIFF\x00\x00\x00\x00WEBPVP8 junk",
    ],
    ids=["svg", "gif", "empty", "text", "short png", "cut png", "cut jpeg", "bad webp"],
)
def test_what_is_not_a_decodable_image_is_refused(data: bytes) -> None:
    with pytest.raises(CoverInvalidError):
        run(data)


def test_the_claimed_side_limit_is_the_configured_one() -> None:
    small = ImageLimits(background_side=2000, bubble_side=100, cover_side=100)

    assert run(images.png((100, 100)), small).width == 100
    with pytest.raises(CoverInvalidError):
        run(images.png((101, 101)), small)


def test_the_same_pixel_ceiling_is_set_for_every_kind() -> None:
    # Two decodes of different kinds running together must not set Pillow's
    # global to different values under each other.
    assert LIMITS.pixel_ceiling == max(2000, 100, 2048, 2048) ** 2
