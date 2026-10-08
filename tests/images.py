"""Builders for test images (Pillow), including hostile ones."""

import io
import struct
import zlib
from typing import Any

from PIL import Image, PngImagePlugin

SECRET_COMMENT = "secret-comment-4711"
SECRET_CAMERA = "SecretCamera9000"


def encode(image: Image.Image, fmt: str, **options: Any) -> bytes:
    out = io.BytesIO()
    image.save(out, fmt, **options)
    return out.getvalue()


def png(
    size: tuple[int, int] = (32, 24),
    mode: str = "RGB",
    color: Any = (200, 30, 30),
    **options: Any,
) -> bytes:
    return encode(Image.new(mode, size, color), "PNG", **options)


def jpeg(size: tuple[int, int] = (32, 24), **options: Any) -> bytes:
    return encode(Image.new("RGB", size, (30, 120, 200)), "JPEG", **options)


def webp(size: tuple[int, int] = (32, 24), **options: Any) -> bytes:
    return encode(Image.new("RGB", size, (30, 200, 120)), "WEBP", **options)


def exif_with_gps(orientation: int = 1) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = SECRET_CAMERA  # Make
    exif[0x0112] = orientation
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (4.0, 36.0, 0.0)
    gps[3] = "W"
    gps[4] = (74.0, 5.0, 0.0)
    return exif


def jpeg_with_metadata(size: tuple[int, int] = (32, 24), orientation: int = 1) -> bytes:
    return jpeg(size, exif=exif_with_gps(orientation).tobytes())


def png_with_metadata() -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", SECRET_COMMENT)
    info.add_itxt("XML:com.adobe.xmp", f"<x:xmpmeta>{SECRET_COMMENT}</x:xmpmeta>")
    return png(pnginfo=info, exif=exif_with_gps().tobytes())


def webp_with_metadata() -> bytes:
    return webp(
        exif=exif_with_gps().tobytes(),
        xmp=f"<x:xmpmeta>{SECRET_COMMENT}</x:xmpmeta>".encode(),
    )


def with_trailing_payload(
    data: bytes, payload: bytes = b"<script>evil()</script>"
) -> bytes:
    """A polyglot: a valid image followed by something else."""
    return data + payload


def svg() -> bytes:
    return b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def gif() -> bytes:
    return encode(Image.new("P", (8, 8)), "GIF")


def animated_webp() -> bytes:
    frames = [Image.new("RGB", (16, 16), c) for c in ((255, 0, 0), (0, 255, 0))]
    return encode(
        frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=50
    )


def animated_png() -> bytes:
    frames = [
        Image.new("RGBA", (16, 16), c) for c in ((255, 0, 0, 255), (0, 255, 0, 255))
    ]
    return encode(
        frames[0], "PNG", save_all=True, append_images=frames[1:], duration=50
    )


def png_header_only(width: int, height: int) -> bytes:
    """A PNG that claims a huge size but carries no pixel data (a "bomb")."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )
