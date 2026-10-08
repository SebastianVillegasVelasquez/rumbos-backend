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
)

THUMBNAIL_MAX_WIDTH = 640
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

    def side_for(self, kind: AssetKind) -> int:
        if kind is AssetKind.BACKGROUND:
            return self.background_side
        return self.bubble_side


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    data: bytes
    mime: str
    width: int
    height: int
    thumbnail: bytes | None  # WebP, backgrounds only


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
    # Pillow warns above this many pixels and refuses at twice as many. No
    # image within the side limit can reach it.
    Image.MAX_IMAGE_PIXELS = max(limits.background_side, limits.bubble_side) ** 2
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
    thumbnail = _thumbnail(image) if kind is AssetKind.BACKGROUND else None
    return ProcessedImage(
        data=data,
        mime=_MIME[fmt],
        width=image.width,
        height=image.height,
        thumbnail=thumbnail,
    )


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
