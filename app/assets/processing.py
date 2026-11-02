"""Validation and re-encoding of uploaded images. Pure and synchronous.

Run it in a worker thread (`asyncio.to_thread`): decoding a large image takes
seconds and must not block the event loop.

Nothing about the upload is trusted: not its Content-Type, not its file name,
not its extension. The format is decided from the bytes, the pixels are
decoded, and what is stored is a fresh encoding of those pixels, so metadata
(EXIF, GPS, XMP, comments, ICC) and anything smuggled after the image data
(polyglot files) never reach storage.
"""

import io
import threading
from dataclasses import dataclass
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.enums import AssetKind
from app.exceptions import (
    AssetDimensionsTooLargeError,
    AssetInvalidImageError,
    AssetTypeNotAllowedError,
    CoverInvalidError,
)

THUMBNAIL_MAX_WIDTH = 640
# A cover is a square no bigger than this (it is shown on a small disc).
COVER_SIDE = 512
_JPEG_QUALITY = 90
_WEBP_QUALITY = 90
_THUMB_QUALITY = 80

# A decode of a maximum-size image holds hundreds of MB; never do many at once.
_DECODE_SLOTS = threading.BoundedSemaphore(2)

_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass(frozen=True, slots=True)
class ImageLimits:
    """Largest accepted side in pixels, per kind."""

    background_side: int
    bubble_side: int
    image_side: int = 2048
    cover_side: int = 2048

    def side_for(self, kind: AssetKind) -> int:
        if kind is AssetKind.BACKGROUND:
            return self.background_side
        if kind is AssetKind.IMAGE:
            return self.image_side
        if kind is AssetKind.COVER:
            return self.cover_side
        return self.bubble_side

    @property
    def pixel_ceiling(self) -> int:
        """Pillow warns above this many pixels and refuses at twice as many. No
        image within the side limit of any kind can reach it."""
        return (
            max(
                self.background_side,
                self.bubble_side,
                self.image_side,
                self.cover_side,
            )
            ** 2
        )


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    data: bytes
    mime: str
    width: int
    height: int
    thumbnail: bytes | None  # WebP, for the kinds that have one


def sniff_format(head: bytes) -> str | None:
    """PNG, JPEG or WEBP by magic number; anything else is not allowed."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return None


def process_image(
    file: BinaryIO, kind: AssetKind, limits: ImageLimits
) -> ProcessedImage:
    """Raises `AssetTypeNotAllowedError`, `AssetInvalidImageError` or
    `AssetDimensionsTooLargeError`; otherwise returns the bytes to store."""
    file.seek(0)
    fmt = sniff_format(file.read(16))
    file.seek(0)
    if fmt is None:
        raise AssetTypeNotAllowedError("only PNG, JPEG and static WebP are accepted")

    max_side = limits.side_for(kind)
    # The same value for every kind, so two decodes running together never set
    # it under each other's feet.
    Image.MAX_IMAGE_PIXELS = limits.pixel_ceiling
    with _DECODE_SLOTS:
        try:
            return _process(file, fmt, kind, max_side)
        except (AssetTypeNotAllowedError, AssetDimensionsTooLargeError):
            raise
        except Image.DecompressionBombError as exc:
            # Raised by Pillow while opening: far more pixels than allowed.
            raise AssetDimensionsTooLargeError(max_side, 0, 0) from exc
        except (
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
            EOFError,
        ) as exc:
            raise AssetInvalidImageError("the image could not be decoded") from exc


def _process(
    file: BinaryIO, fmt: str, kind: AssetKind, max_side: int
) -> ProcessedImage:
    with Image.open(file, formats=[fmt]) as opened:
        width, height = opened.size
        # Judge the size from the header, before decoding a single pixel.
        if max(width, height) > max_side:
            raise AssetDimensionsTooLargeError(max_side, width, height)
        if getattr(opened, "n_frames", 1) > 1:
            raise AssetTypeNotAllowedError("animated images are not accepted")
        opened.load()
        image = ImageOps.exif_transpose(opened)  # honor the orientation, then drop EXIF

    image = _normalize_mode(image)
    image.info.clear()  # nothing from the source may be written back out
    data = _encode(image, fmt)
    thumbnail = _thumbnail(image) if kind.has_thumbnail else None
    return ProcessedImage(
        data=data,
        mime=_MIME[fmt],
        width=image.width,
        height=image.height,
        thumbnail=thumbnail,
    )


@dataclass(frozen=True, slots=True)
class ProcessedCover:
    data: bytes  # WebP
    width: int
    height: int


def process_cover(file: BinaryIO, limits: ImageLimits) -> ProcessedCover:
    """A track's cover: decoded, centre-cropped to a square, shrunk to at most
    `COVER_SIDE` px a side, stripped of every piece of metadata and encoded as
    WebP. Whatever the source was (a file the editor chose, or a picture found
    inside the audio), none of its bytes are kept.

    Raises `CoverInvalidError` for anything that is not a static PNG, JPEG or
    WebP within `limits.cover_side`, or that does not decode."""
    file.seek(0)
    fmt = sniff_format(file.read(16))
    file.seek(0)
    if fmt is None:
        raise CoverInvalidError("not a PNG, JPEG or WebP image")

    Image.MAX_IMAGE_PIXELS = limits.pixel_ceiling
    with _DECODE_SLOTS:
        try:
            return _process_cover(file, fmt, limits.cover_side)
        except CoverInvalidError:
            raise
        except Image.DecompressionBombError as exc:
            raise CoverInvalidError("far more pixels than allowed") from exc
        except (
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
            EOFError,
        ) as exc:
            raise CoverInvalidError("the image could not be decoded") from exc


def _process_cover(file: BinaryIO, fmt: str, max_side: int) -> ProcessedCover:
    with Image.open(file, formats=[fmt]) as opened:
        width, height = opened.size
        # Judge the size from the header, before decoding a single pixel.
        if max(width, height) > max_side:
            raise CoverInvalidError(f"{width}x{height} exceeds {max_side} px a side")
        if getattr(opened, "n_frames", 1) > 1:
            raise CoverInvalidError("animated images are not accepted")
        opened.load()
        image = ImageOps.exif_transpose(opened)  # honor the orientation, then drop EXIF

    image = _centre_square(_normalize_mode(image))
    if image.width > COVER_SIDE:
        image = image.resize(
            (COVER_SIDE, COVER_SIDE), Image.Resampling.LANCZOS, reducing_gap=2.0
        )
    image.info.clear()  # nothing from the source may be written back out
    out = io.BytesIO()
    image.save(out, "WEBP", quality=_WEBP_QUALITY, method=4)
    return ProcessedCover(out.getvalue(), image.width, image.height)


def _centre_square(image: Image.Image) -> Image.Image:
    """The largest centred square of the image (never scaled up)."""
    side = min(image.size)
    left, top = (image.width - side) // 2, (image.height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _normalize_mode(image: Image.Image) -> Image.Image:
    """RGB or RGBA: palette, grayscale, CMYK and friends become plain colour."""
    if image.mode in ("I", "I;16", "I;16L", "I;16B"):
        # 16-bit grayscale: scale down instead of clipping to white.
        image = image.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    target = "RGBA" if has_alpha else "RGB"
    return image if image.mode == target else image.convert(target)


def _encode(image: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    if fmt == "JPEG":
        # JPEG has no alpha; `_normalize_mode` only keeps it for sources that had it.
        image.convert("RGB").save(out, "JPEG", quality=_JPEG_QUALITY, optimize=True)
    elif fmt == "WEBP":
        image.save(out, "WEBP", quality=_WEBP_QUALITY, method=4)
    else:
        image.save(out, "PNG", optimize=True)
    return out.getvalue()


def _thumbnail(image: Image.Image) -> bytes:
    small = image
    if image.width > THUMBNAIL_MAX_WIDTH:
        height = max(1, round(image.height * THUMBNAIL_MAX_WIDTH / image.width))
        small = image.resize(
            (THUMBNAIL_MAX_WIDTH, height), Image.Resampling.LANCZOS, reducing_gap=2.0
        )
    out = io.BytesIO()
    small.save(out, "WEBP", quality=_THUMB_QUALITY, method=4)
    return out.getvalue()
