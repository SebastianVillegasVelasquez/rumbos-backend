"""Display metadata of an upload: file name, title, credit.

Everything here is text for people to read. None of it is ever used to build a
path (stored files are named by asset id only), so sanitizing is about keeping
control characters and path noise out of what the UI shows.
"""

import unicodedata
from pathlib import PurePosixPath

from app.enums import AssetKind
from app.schemas.asset import (
    CREDIT_MAX_LENGTH,
    FILENAME_MAX_LENGTH,
    TITLE_MAX_LENGTH,
)

_DEFAULT_TITLES = {
    AssetKind.BACKGROUND: "Fondo",
    AssetKind.BUBBLE: "Imagen",
    AssetKind.AUDIO: "Audio",
    AssetKind.IMAGE: "Imagen",
}


def _printable(text: str) -> str:
    """Control and format characters dropped, whitespace runs collapsed.

    (`str.isspace` is true for some control characters such as U+001F, so the
    category decides, not `isspace`.)
    """
    kept: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch in "\t\n\r\f\v" or category.startswith("Z"):
            kept.append(" ")
        elif category not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            kept.append(ch)
    return " ".join("".join(kept).split())


def clean_filename(raw: str | None) -> str | None:
    """The base name of what the client sent (any folder part dropped), or None."""
    if not raw:
        return None
    base = PurePosixPath(raw.replace("\\", "/")).name
    cleaned = _printable(base)[:FILENAME_MAX_LENGTH].strip()
    return cleaned or None


def default_title(filename: str | None, kind: AssetKind) -> str:
    """The file name without its extension, or a generic word if there is none."""
    cleaned = clean_filename(filename)
    if cleaned:
        stem = PurePosixPath(cleaned).stem.strip()
        if stem:
            return stem[:TITLE_MAX_LENGTH].strip()
    return _DEFAULT_TITLES[kind]


def clean_credit(raw: str | None) -> str | None:
    """Trimmed credit; empty means none. Too long is the caller's error to raise."""
    if raw is None:
        return None
    cleaned = _printable(raw)
    return cleaned or None


def fits_credit(credit: str | None) -> bool:
    return credit is None or len(credit) <= CREDIT_MAX_LENGTH
