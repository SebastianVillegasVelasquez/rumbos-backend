import time

import pytest

from app.trainer.language import ENGLISH, MAX_FREE_TEXT_CHARS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Hello   World!  ", "hello world"),
        ("It’s a boat.", "it's a boat"),
        ("IT‘S", "it's"),
        ("Where are you from?", "where are you from"),
        ("Yes, please...", "yes, please"),
        ("", ""),
        ("   ", ""),
        ("A\tB\nC", "a b c"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert ENGLISH.normalize(raw) == expected


def test_curly_and_straight_apostrophes_normalize_alike() -> None:
    assert ENGLISH.normalize("don’t") == ENGLISH.normalize("don't")


@pytest.mark.parametrize(
    ("raw", "tokens"),
    [
        ("The tour leaves at 7:15.", ["the", "tour", "leaves", "at", "7:15"]),
        ("It costs 12,000 pesos", ["it", "costs", "12,000", "pesos"]),
        ("I don't know", ["i", "don't", "know"]),
        ("three-oh-four", ["three-oh-four"]),
        ("'quoted'", ["quoted"]),
        ("a - b", ["a", "b"]),
        ("", []),
        ("!!!", []),
    ],
)
def test_tokenize(raw: str, tokens: list[str]) -> None:
    assert ENGLISH.tokenize(raw) == tokens


def test_text_is_capped_before_processing() -> None:
    long = "a " * 5000
    assert len(ENGLISH.normalize(long)) <= MAX_FREE_TEXT_CHARS
    assert len(ENGLISH.tokenize(long)) <= MAX_FREE_TEXT_CHARS


def test_hostile_input_is_fast() -> None:
    start = time.perf_counter()
    ENGLISH.tokenize("'" * 100_000 + "-" * 100_000 + "a" * 100_000)
    ENGLISH.normalize("’" * 100_000)
    assert time.perf_counter() - start < 0.5
