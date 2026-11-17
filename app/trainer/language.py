"""What the grader needs to know about a language, and nothing else.

`LanguageRules` is a small Strategy: normalize a student's text and split it
into tokens. English is the only implementation. Everything the student types
is capped before any processing, and tokenizing walks characters instead of
running regular expressions over the input, so no input can make it slow.
"""

import unicodedata
from typing import Protocol

# Free text from a student is cut to this many characters before anything else.
MAX_FREE_TEXT_CHARS = 300

_APOSTROPHES = {"’": "'", "‘": "'", "ʼ": "'", "`": "'", "´": "'"}
_TERMINAL = ".!?,;:¡¿"
# Kept inside a number so that 7:15, 12,000 and 3.50 stay one token.
_NUMBER_JOINERS = ":.,"


class LanguageRules(Protocol):
    code: str

    def normalize(self, text: str) -> str:
        """Comparable form of a text: capped, case-folded, apostrophes
        unified, whitespace collapsed, terminal punctuation removed."""
        ...

    def tokenize(self, text: str) -> list[str]:
        """The words and numbers of a text, in order, already normalized."""
        ...

    def tokenize_cased(self, text: str) -> list[str]:
        """The same words as `tokenize`, with the student's capitals kept."""
        ...


def _joins(ch: str, before: str, after: str) -> bool:
    """Whether a separator character belongs inside the word or number it sits in."""
    if ch in "'-":
        return before.isalpha() and after.isalpha()
    if ch in _NUMBER_JOINERS:
        return before.isdigit() and after.isdigit()
    return False


class EnglishRules:
    code = "en"

    def normalize(self, text: str) -> str:
        text = unicodedata.normalize("NFKC", text[:MAX_FREE_TEXT_CHARS])
        text = "".join(_APOSTROPHES.get(ch, ch) for ch in text).casefold()
        text = " ".join(text.split())
        return text.rstrip(_TERMINAL + " ")

    def tokenize(self, text: str) -> list[str]:
        return _split(self.normalize(text))

    def tokenize_cased(self, text: str) -> list[str]:
        text = unicodedata.normalize("NFKC", text[:MAX_FREE_TEXT_CHARS])
        return _split("".join(_APOSTROPHES.get(ch, ch) for ch in text))


def _split(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for index, ch in enumerate(text):
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if ch.isalnum() or _joins(ch, before, after):
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


ENGLISH = EnglishRules()
