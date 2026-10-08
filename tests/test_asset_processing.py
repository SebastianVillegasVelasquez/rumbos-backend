"""The upload pipeline's image handling: decoding, limits, re-encoding."""

import io
from collections.abc import Callable

import pytest
from PIL import Image

from app.assets.processing import (
    THUMBNAIL_MAX_WIDTH,
    ImageLimits,
    ProcessedImage,
    process_image,
)
from app.enums import AssetKind
from app.exceptions import (
    AssetDimensionsTooLargeError,
    AssetInvalidImageError,
    AssetTypeNotAllowedError,
)
from tests import images

LIMITS = ImageLimits(background_side=2000, bubble_side=100)


def run(data: bytes, kind: AssetKind = AssetKind.BUBBLE) -> ProcessedImage:
    return process_image(io.BytesIO(data), kind, LIMITS)


def reopen(processed: ProcessedImage) -> Image.Image:
    image = Image.open(io.BytesIO(processed.data))
    image.load()
    return image


@pytest.mark.parametrize(
    ("data", "mime", "fmt"),
    [
        (images.png(), "image/png", "PNG"),
        (images.jpeg(), "image/jpeg", "JPEG"),
        (images.webp(), "image/webp", "WEBP"),
    ],
)
def test_allowed_formats_keep_their_format(data: bytes, mime: str, fmt: str) -> None:
    processed = run(data)

    assert processed.mime == mime
    assert reopen(processed).format == fmt
    assert (processed.width, processed.height) == (32, 24)


@pytest.mark.parametrize(
    "data",
    [
        images.svg(),
        images.gif(),
        b"just some text pretending to be a picture",
        b"",
        b"\x00" * 64,
        b"RIFF\x00\x00\x00\x00WAVEfmt ",  # RIFF, but not WebP
    ],
)
def test_other_formats_are_not_allowed(data: bytes) -> None:
    with pytest.raises(AssetTypeNotAllowedError):
        run(data)


@pytest.mark.parametrize("data", [images.animated_webp(), images.animated_png()])
def test_animations_are_not_allowed(data: bytes) -> None:
    with pytest.raises(AssetTypeNotAllowedError):
        run(data)


@pytest.mark.parametrize("data", [images.png(), images.jpeg(), images.webp()])
def test_a_truncated_image_is_invalid(data: bytes) -> None:
    with pytest.raises(AssetInvalidImageError):
        run(data[: len(data) // 2])


def test_a_png_signature_with_garbage_is_invalid() -> None:
    with pytest.raises(AssetInvalidImageError):
        run(b"\x89PNG\r\n\x1a\n" + b"not really a png" * 10)


def test_side_limit_depends_on_the_kind() -> None:
    big = images.png((150, 20))

    run(big, AssetKind.BACKGROUND)  # fine as a background
    with pytest.raises(AssetDimensionsTooLargeError) as caught:
        run(big, AssetKind.BUBBLE)

    assert (caught.value.limit_side, caught.value.width, caught.value.height) == (
        100,
        150,
        20,
    )


@pytest.mark.parametrize("size", [(60000, 60000), (9000, 9000), (3000, 3000)])
def test_decompression_bombs_are_refused_without_decoding(
    size: tuple[int, int],
) -> None:
    """Only a header: refused from the size alone (and nothing is allocated)."""
    with pytest.raises(AssetDimensionsTooLargeError):
        run(images.png_header_only(*size), AssetKind.BACKGROUND)


@pytest.mark.parametrize(
    ("data", "secrets"),
    [
        (
            images.jpeg_with_metadata(),
            [images.SECRET_CAMERA.encode(), b"Exif", b"GPS"],
        ),
        (
            images.png_with_metadata(),
            [
                images.SECRET_COMMENT.encode(),
                images.SECRET_CAMERA.encode(),
                b"tEXt",
                b"iTXt",
                b"eXIf",
            ],
        ),
        (
            images.webp_with_metadata(),
            [
                images.SECRET_COMMENT.encode(),
                images.SECRET_CAMERA.encode(),
                b"EXIF",
                b"XMP ",
            ],
        ),
    ],
)
def test_metadata_is_gone_from_the_stored_bytes(
    data: bytes, secrets: list[bytes]
) -> None:
    # The input really carries the metadata (so the test can fail).
    assert any(secret in data for secret in secrets)

    stored = run(data).data

    for secret in secrets:
        assert secret not in stored
    reloaded = Image.open(io.BytesIO(stored))
    assert dict(reloaded.getexif()) == {}
    assert not {"exif", "xmp", "comment", "icc_profile"} & set(reloaded.info)


@pytest.mark.parametrize("payload", [b"<script>evil()</script>", b"PK\x03\x04zip"])
@pytest.mark.parametrize("make", [images.png, images.jpeg])
def test_trailing_data_does_not_survive_re_encoding(
    make: Callable[[], bytes], payload: bytes
) -> None:
    polyglot = images.with_trailing_payload(make(), payload)

    stored = run(polyglot).data

    assert payload not in stored


def test_exif_orientation_is_applied_before_it_is_dropped() -> None:
    sideways = images.jpeg_with_metadata((40, 20), orientation=6)  # rotate 90 CW

    processed = run(sideways, AssetKind.BACKGROUND)

    assert (processed.width, processed.height) == (20, 40)
    assert reopen(processed).size == (20, 40)


def test_palette_images_with_transparency_become_rgba() -> None:
    palette = Image.new("P", (8, 8), 0)
    palette.info["transparency"] = 0
    data = images.encode(palette, "PNG", transparency=0)

    assert reopen(run(data)).mode == "RGBA"


def test_opaque_palette_and_grayscale_become_rgb() -> None:
    for mode in ("P", "L", "1"):
        data = images.encode(Image.new(mode, (8, 8)), "PNG")
        assert reopen(run(data)).mode == "RGB"


def test_cmyk_jpeg_becomes_rgb() -> None:
    data = images.encode(Image.new("CMYK", (8, 8), (0, 255, 255, 0)), "JPEG")

    stored = reopen(run(data))

    assert stored.mode == "RGB"
    red = stored.getpixel((0, 0))
    assert isinstance(red, tuple) and red[0] > 200  # red-ish, not inverted


def test_sixteen_bit_grayscale_is_scaled_not_clipped() -> None:
    mid = Image.new("I;16", (8, 8), 32768)
    data = images.encode(mid, "PNG")

    value = reopen(run(data)).convert("L").getpixel((0, 0))

    assert isinstance(value, int)
    assert 100 < value < 156


def test_rgba_keeps_its_alpha_channel() -> None:
    translucent = images.png(mode="RGBA", color=(255, 0, 0, 90))

    assert reopen(run(translucent)).getpixel((0, 0)) == (255, 0, 0, 90)


def test_backgrounds_get_a_webp_thumbnail_at_most_640_wide() -> None:
    wide = images.png((1600, 900))

    processed = run(wide, AssetKind.BACKGROUND)

    assert processed.thumbnail is not None
    thumb = Image.open(io.BytesIO(processed.thumbnail))
    assert thumb.format == "WEBP"
    assert thumb.size == (THUMBNAIL_MAX_WIDTH, 360)  # aspect ratio kept


def test_small_backgrounds_are_not_enlarged_for_the_thumbnail() -> None:
    processed = run(images.png((300, 200)), AssetKind.BACKGROUND)

    assert processed.thumbnail is not None
    assert Image.open(io.BytesIO(processed.thumbnail)).size == (300, 200)


def test_bubble_images_have_no_thumbnail() -> None:
    assert run(images.png()).thumbnail is None


def test_same_pixels_give_the_same_bytes() -> None:
    """De-duplication hashes the stored bytes, so encoding must be stable."""
    assert run(images.png()).data == run(images.png()).data
