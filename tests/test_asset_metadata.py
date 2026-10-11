"""Sanitizing of an upload's display metadata (pure functions)."""

import pytest

from app.assets.metadata import (
    clean_credit,
    clean_filename,
    default_title,
    fits_credit,
)
from app.enums import AssetKind


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("foto.png", "foto.png"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\ana\\foto.png", "foto.png"),
        ("a\x00b\x1f\x7f.png", "ab.png"),
        ("tab\tand\nnewline.png", "tab and newline.png"),
        ("\u202egnp.exe", "gnp.exe"),  # bidi override is a format character
        ("   ", None),
        ("", None),
        (None, None),
        ("/", None),
    ],
)
def test_clean_filename(raw: str | None, expected: str | None) -> None:
    assert clean_filename(raw) == expected


def test_clean_filename_is_capped_at_255_characters() -> None:
    cleaned = clean_filename("x" * 400 + ".png")

    assert cleaned is not None and len(cleaned) == 255


@pytest.mark.parametrize(
    ("filename", "kind", "expected"),
    [
        ("Playa.v2.png", AssetKind.BACKGROUND, "Playa.v2"),
        ("sin extension", AssetKind.BUBBLE, "sin extension"),
        (None, AssetKind.BACKGROUND, "Fondo"),
        (None, AssetKind.BUBBLE, "Imagen"),
        ("", AssetKind.BUBBLE, "Imagen"),
    ],
)
def test_default_title(filename: str | None, kind: AssetKind, expected: str) -> None:
    assert default_title(filename, kind) == expected


def test_default_title_fits_a_title() -> None:
    assert len(default_title("x" * 300 + ".png", AssetKind.BUBBLE)) <= 120


def test_clean_credit() -> None:
    assert clean_credit("  Foto:   Ana\x00 ") == "Foto: Ana"
    assert clean_credit("   ") is None
    assert clean_credit(None) is None
    assert fits_credit("c" * 300) and not fits_credit("c" * 301)
