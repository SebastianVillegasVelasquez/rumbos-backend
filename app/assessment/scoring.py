"""Exact scoring of an assessment. Pure: no I/O, no floats.

Every quantity is a `Fraction`. The pass decision compares the exact total
with the pass mark; the two-decimal string a student sees is derived from the
exact value afterwards and is never what is compared. (With this form the
total is always a multiple of 1/6 of a percent, so a value that displays as
80.00 is exactly 80.)

Total = sum over sections of weight x points / max, as a percentage.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Literal

PassOperator = Literal["gt", "gte"]
Band = Literal["emerging", "developing", "consolidating", "achieved"]

HUNDRED = Fraction(100)
# Lower bounds (percent) of the bands of the document: 0-39, 40-59, 60-79, 80-100.
_BANDS: tuple[tuple[Fraction, Band], ...] = (
    (Fraction(80), "achieved"),
    (Fraction(60), "consolidating"),
    (Fraction(40), "developing"),
)


@dataclass(frozen=True)
class SectionScore:
    key: str
    points: Fraction
    max: int
    weight: int

    @property
    def fraction(self) -> Fraction:
        return self.points / self.max if self.max else Fraction(0)

    @property
    def percent(self) -> Fraction:
        return self.fraction * HUNDRED


@dataclass(frozen=True)
class Total:
    percent: Fraction
    sections: tuple[SectionScore, ...]

    @property
    def band(self) -> Band:
        return band_of(self.percent)


def total_of(sections: Sequence[SectionScore]) -> Total:
    percent = sum((s.fraction * s.weight for s in sections), Fraction(0))
    return Total(percent, tuple(sections))


def band_of(percent: Fraction) -> Band:
    for lower, band in _BANDS:
        if percent >= lower:
            return band
    return "emerging"


def passes(
    percent: Fraction, pass_percent: int | Fraction, operator: PassOperator
) -> bool:
    """`gt`: strictly above the mark (80.00 fails, 80.17 passes). `gte`: at or
    above. Both on the exact value, never on a rounded one."""
    mark = Fraction(pass_percent)
    return percent > mark if operator == "gt" else percent >= mark


def decimal_string(value: Fraction) -> str:
    """Two decimals, half up, for display only."""
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    return str(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def module_percents(
    earned: Mapping[str, Fraction], available: Mapping[str, Fraction]
) -> dict[str, Fraction]:
    """Percent per curriculum module from points earned and available there."""
    return {
        module: (earned.get(module, Fraction(0)) / total) * HUNDRED
        for module, total in available.items()
        if total
    }
