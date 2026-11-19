"""The deterministic proxy rubric for Writing and Speaking.

The client's rubric is for a human reader. This is an approximation made of
named, mechanical checks, defined as data in the pack (`rubrics.json`): each
criterion lists the checks that award its points, and each check returns the
share (0 to 1) of what it looks at that passes. A criterion scores
`points x mean(checks)`, so a task scores 0 to 5 as an exact `Fraction`.

It cannot judge meaning. It cannot tell a well-written wrong sentence from a
right one, and it rewards any capitalized word in a name position. Those
limits are listed in the report and are why the rubric needs pedagogical
sign-off. It never calls a model; the same response always scores the same.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.trainer.language import MAX_FREE_TEXT_CHARS, LanguageRules

TEXT = "@text"  # `on` target: the whole response read as one text
_PLACEHOLDERS = frozenset(
    {"n/a", "na", "none", "null", "test", "testing", "xxx", "xx", "asdf", "abc", "no"}
)
_NUMBER_WORDS = frozenset(
    [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
    ]
)
ONE = Fraction(1)
ZERO = Fraction(0)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckSpec(_Frozen):
    check: Literal[
        "field_not_empty",
        "not_placeholder",
        "starts_with_capital",
        "word_count_between",
        "template_phrases_present",
        "no_blank_left",
        "uses_model_phrase",
        "token_in_course_vocabulary_or_capitalized_name",
        "values_distinct",
        "contains_number",
        "digits_at_least",
        "sentence_terminals_at_least",
        "past_verbs_at_least",
        "be_plus_ing",
    ]
    # Which fields, blanks or answers it looks at: ids, "*" for all, or "@text".
    on: list[str] = Field(default_factory=lambda: ["*"])
    phrases: list[str] = Field(default_factory=list)
    min: int | None = None
    max: int | None = None
    # A capitalized first word of a field counts as a name (fields only).
    allow_sentence_initial: bool = False
    # Scales the criterion instead of being averaged into it: a criterion whose
    # words could come from the template itself must be multiplied by how much
    # of the template the student actually filled in.
    multiplier: bool = False


class Criterion(_Frozen):
    id: str
    points: int = Field(ge=1)
    checks: list[CheckSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _has_a_scored_check(self) -> "Criterion":
        if all(c.multiplier for c in self.checks):
            raise ValueError(f"{self.id}: needs at least one non-multiplier check")
        return self


class Rubric(_Frozen):
    id: str
    criteria: list[Criterion] = Field(min_length=1)

    def max_points(self) -> int:
        return sum(c.points for c in self.criteria)


@dataclass(frozen=True)
class Response:
    """What a student wrote, ready to be checked."""

    # id -> text, in the item's order (fields, blanks or answers).
    values: Mapping[str, str]
    # The response read as one text (blanks filled into the template).
    text: str


@dataclass(frozen=True)
class Context:
    response: Response
    vocabulary: frozenset[str]
    rules: LanguageRules

    def targets(self, spec: CheckSpec) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for ref in spec.on:
            if ref == "*":
                out.extend(self.response.values.items())
            elif ref == TEXT:
                out.append((TEXT, self.response.text))
            else:
                out.append((ref, self.response.values.get(ref, "")))
        return out


def _clean(value: str) -> str:
    return value[:MAX_FREE_TEXT_CHARS].strip()


def _share(passed: int, total: int) -> Fraction:
    return Fraction(passed, total) if total else ZERO


def is_placeholder(value: str, rules: LanguageRules) -> bool:
    """Empty, blanks, punctuation only, one repeated character, or a stock
    filler word."""
    text = _clean(value)
    tokens = rules.tokenize(text)
    if not tokens or rules.normalize(text) in _PLACEHOLDERS:
        return True
    if len(tokens) == 1:
        word = tokens[0]
        if word in _PLACEHOLDERS or len(set(word)) == 1 and len(word) > 2:
            return True
    return False


def _field_not_empty(ctx: Context, spec: CheckSpec) -> Fraction:
    items = ctx.targets(spec)
    return _share(sum(1 for _, v in items if _clean(v)), len(items))


def _not_placeholder(ctx: Context, spec: CheckSpec) -> Fraction:
    items = ctx.targets(spec)
    return _share(
        sum(1 for _, v in items if not is_placeholder(v, ctx.rules)), len(items)
    )


def _starts_with_capital(ctx: Context, spec: CheckSpec) -> Fraction:
    items = ctx.targets(spec)
    ok = 0
    for _, value in items:
        first = next((c for c in _clean(value) if c.isalpha()), "")
        ok += first.isupper()
    return _share(ok, len(items))


def _word_count_between(ctx: Context, spec: CheckSpec) -> Fraction:
    low, high = spec.min or 0, spec.max if spec.max is not None else 10**9
    items = ctx.targets(spec)
    ok = sum(1 for _, v in items if low <= len(ctx.rules.tokenize(v)) <= high)
    return _share(ok, len(items))


def _contains(tokens: Sequence[str], phrase: Sequence[str]) -> bool:
    n = len(phrase)
    return n > 0 and any(
        list(tokens[i : i + n]) == list(phrase) for i in range(len(tokens) - n + 1)
    )


def _template_phrases_present(ctx: Context, spec: CheckSpec) -> Fraction:
    tokens = [t for _, v in ctx.targets(spec) for t in ctx.rules.tokenize(v)]
    phrases = [ctx.rules.tokenize(p) for p in spec.phrases]
    return _share(sum(1 for p in phrases if _contains(tokens, p)), len(phrases))


def _no_blank_left(ctx: Context, spec: CheckSpec) -> Fraction:
    items = ctx.targets(spec)
    ok = all(_clean(v) and "_" not in v for _, v in items)
    return ONE if ok and items else ZERO


def _uses_model_phrase(ctx: Context, spec: CheckSpec) -> Fraction:
    """Each target counts when it holds at least one of the phrases."""
    phrases = [ctx.rules.tokenize(p) for p in spec.phrases]
    items = ctx.targets(spec)
    ok = 0
    for _, value in items:
        tokens = ctx.rules.tokenize(value)
        ok += any(_contains(tokens, p) for p in phrases)
    return _share(ok, len(items))


def _known_words(ctx: Context, spec: CheckSpec) -> Fraction:
    known = total = 0
    for _, value in ctx.targets(spec):
        cased = ctx.rules.tokenize_cased(value)
        for index, word in enumerate(cased):
            total += 1
            folded = ctx.rules.normalize(word)
            if folded in ctx.vocabulary or any(c.isdigit() for c in word):
                known += 1
            elif word[:1].isupper() and (index > 0 or spec.allow_sentence_initial):
                known += 1  # a name: not in the course list, but capitalized
    return _share(known, total)


def _values_distinct(ctx: Context, spec: CheckSpec) -> Fraction:
    values = [ctx.rules.normalize(v) for _, v in ctx.targets(spec) if _clean(v)]
    return ONE if values and len(set(values)) == len(values) else ZERO


def _contains_number(ctx: Context, spec: CheckSpec) -> Fraction:
    items = ctx.targets(spec)
    ok = 0
    for _, value in items:
        tokens = ctx.rules.tokenize(value)
        ok += any(any(c.isdigit() for c in t) or t in _NUMBER_WORDS for t in tokens)
    return _share(ok, len(items))


def _digits_at_least(ctx: Context, spec: CheckSpec) -> Fraction:
    items = ctx.targets(spec)
    ok = sum(
        1 for _, v in items if sum(c.isdigit() for c in _clean(v)) >= (spec.min or 1)
    )
    return _share(ok, len(items))


def _terminals_at_least(ctx: Context, spec: CheckSpec) -> Fraction:
    wanted = spec.min or 1
    count = sum(_clean(v).count(c) for _, v in ctx.targets(spec) for c in ".!?")
    return min(ONE, Fraction(count, wanted))


_BE_FORMS = frozenset(
    {"am", "is", "are", "i'm", "he's", "she's", "it's", "we're", "they're", "you're"}
)


def _past_verbs(ctx: Context, spec: CheckSpec) -> Fraction:
    """How many past-tense verbs the answer holds, against `min`: a word ending
    in -ed, or one of the irregular forms listed in `phrases`. It cannot tell a
    verb from a noun that ends in -ed, which is why it is a proxy."""
    irregular = {p.casefold() for p in spec.phrases}
    count = 0
    for _, value in ctx.targets(spec):
        for token in ctx.rules.tokenize(value):
            count += token in irregular or (len(token) > 3 and token.endswith("ed"))
    return min(ONE, Fraction(count, spec.min or 1))


def _be_plus_ing(ctx: Context, spec: CheckSpec) -> Fraction:
    """Each target counts when it holds a form of *be* followed by a word in
    -ing (the present continuous). Words of four letters or fewer (ring, king)
    do not count as the participle."""
    items = ctx.targets(spec)
    ok = 0
    for _, value in items:
        tokens = ctx.rules.tokenize(value)
        ok += any(
            a in _BE_FORMS and len(b) > 4 and b.endswith("ing")
            for a, b in pairwise(tokens)
        )
    return _share(ok, len(items))


CHECKS: dict[str, Callable[[Context, CheckSpec], Fraction]] = {
    "field_not_empty": _field_not_empty,
    "not_placeholder": _not_placeholder,
    "starts_with_capital": _starts_with_capital,
    "word_count_between": _word_count_between,
    "template_phrases_present": _template_phrases_present,
    "no_blank_left": _no_blank_left,
    "uses_model_phrase": _uses_model_phrase,
    "token_in_course_vocabulary_or_capitalized_name": _known_words,
    "values_distinct": _values_distinct,
    "contains_number": _contains_number,
    "digits_at_least": _digits_at_least,
    "sentence_terminals_at_least": _terminals_at_least,
    "past_verbs_at_least": _past_verbs,
    "be_plus_ing": _be_plus_ing,
}


@dataclass(frozen=True)
class RubricScore:
    points: Fraction
    max: int
    # Criterion id -> points awarded, for an auditor.
    by_criterion: Mapping[str, Fraction]


def score(
    rubric: Rubric,
    response: Response,
    vocabulary: frozenset[str],
    rules: LanguageRules,
) -> RubricScore:
    ctx = Context(response, vocabulary, rules)
    by_criterion: dict[str, Fraction] = {}
    for criterion in rubric.criteria:
        shares = [
            CHECKS[spec.check](ctx, spec)
            for spec in criterion.checks
            if not spec.multiplier
        ]
        scale = ONE
        for spec in criterion.checks:
            if spec.multiplier:
                scale *= CHECKS[spec.check](ctx, spec)
        by_criterion[criterion.id] = (
            criterion.points * scale * sum(shares, ZERO) / len(shares)
        )
    return RubricScore(
        sum(by_criterion.values(), ZERO), rubric.max_points(), by_criterion
    )


def fill_template(template: str, values: Sequence[str]) -> str:
    """The template with its `______` blanks replaced, in order, by what the
    student wrote; a blank left unanswered stays as `______`."""
    parts = template.split("______")
    out = [parts[0]]
    for index, tail in enumerate(parts[1:]):
        given = _clean(values[index]) if index < len(values) else ""
        out.append(given or "______")
        out.append(tail)
    return "".join(out)


def blank_ids(template: str) -> list[str]:
    return [f"b{i + 1}" for i in range(template.count("______"))]


def vocabulary_from(
    texts: Sequence[str], extra: Sequence[str], rules: LanguageRules
) -> frozenset[str]:
    """Every word of the course: those in the exam's own text plus the
    curated list of course words."""
    words: set[str] = set()
    for text in (*texts, *extra):
        words.update(rules.tokenize(text))
    return frozenset(words)


def rubric_from(data: Mapping[str, Any]) -> Rubric:
    return Rubric.model_validate(data)
