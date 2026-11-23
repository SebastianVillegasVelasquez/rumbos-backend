"""The exam definition the Moodle mirror builds its native quiz from. Pure.

It is the only place where the correct answers of a form leave the form's own
shape, and it is only ever put inside a signed server-to-plugin statement
(`app.mirror.statements`), never in a response a student can read.

Every scorable unit of the form is one *question* of the mirror: a choice item
is one, a matching item is one per pair (`<item_id>:<clip index>`), a written
task is one worth 5. Each carries `weight_points`, its exact share of the 100
of the total (`section weight x max points / section max points`), so that
summing `points x weight_points / max_points` over the questions gives the very
percentage the backend scores. The rubric of a written task, its vocabulary and
checks, and every listening transcript stay out: the mirror gets the points the
backend awarded, never a way to recompute them.

`definition_hash` is the sha256 of the canonical JSON of the definition. It is
the form's own identity for the mirror, independent of the pack's
`content_version` (which changes when any file of the pack does), and it
changes exactly when something the mirror shows or scores changes.
"""

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

from app.trainer.assessment_form import (
    AssessmentForm,
    AssessmentItemDef,
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
    Section,
)

SCHEMA = 1
WRITTEN_POINTS = 5


@dataclass(frozen=True)
class Unit:
    """One scorable unit: what a question of the mirror stands for."""

    question_id: str
    section: Section
    item: AssessmentItemDef
    # Matching only: the clip index and the place it belongs to.
    pair: tuple[int, str] | None
    max_points: int

    @property
    def weight_points(self) -> Fraction:
        return Fraction(
            self.section.weight * self.max_points, self.section.max_points()
        )


def pair_question_id(item_id: str, clip_index: int) -> str:
    return f"{item_id}:{clip_index}"


def units(form: AssessmentForm) -> list[Unit]:
    """Every scorable unit of the form, in form order."""
    found: list[Unit] = []
    for section in form.sections:
        for item in section.items():
            if isinstance(item, ChoiceAssessmentItem):
                found.append(Unit(item.item_id, section, item, None, 1))
            elif isinstance(item, MatchAudioItem):
                for clip_index, place in item.key:
                    found.append(
                        Unit(
                            pair_question_id(item.item_id, clip_index),
                            section,
                            item,
                            (clip_index, place),
                            1,
                        )
                    )
            else:
                found.append(Unit(item.item_id, section, item, None, WRITTEN_POINTS))
    return found


def fraction_text(value: Fraction) -> str:
    """`5/3`, or `2` when whole: exact, and what a mirror must keep."""
    return str(value.numerator) if value.denominator == 1 else str(value)


def decimal_text(value: Fraction, places: int = 6) -> str:
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    return str(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _shared(part_shared: Any) -> dict[str, Any] | None:
    if part_shared is None:
        return None
    return {
        "kind": part_shared.kind,
        "title": part_shared.title,
        "text": part_shared.text,
        "image_id": part_shared.image_id,
        "alt_text": part_shared.alt_text,
    }


def _weights(unit: Unit) -> dict[str, Any]:
    return {
        "max_points": unit.max_points,
        "weight_points": fraction_text(unit.weight_points),
        "weight_points_decimal": decimal_text(unit.weight_points),
    }


def _question(unit: Unit) -> dict[str, Any]:
    item = unit.item
    base: dict[str, Any] = {
        "question_id": unit.question_id,
        "item_id": item.item_id,
        "type": item.type,
        "prompt": item.prompt,
    }
    if isinstance(item, ChoiceAssessmentItem):
        base.update(
            options=[{"id": o.id, "text": o.text} for o in item.options],
            correct=item.key,
            audio_id=item.audio.audio if item.audio else None,
            gap_label=item.gap_label,
        )
    elif isinstance(item, MatchAudioItem):
        assert unit.pair is not None
        clip_index, place = unit.pair
        base.update(
            type="match_pair",
            clip=clip_index + 1,
            options=[{"id": p.id, "text": p.text} for p in item.places],
            correct=place,
            audio_id=item.clips[clip_index].audio,
            gap_label=None,
        )
    else:
        assert isinstance(item, ProductiveItem)
        base["written"] = {
            "fields": [{"id": f.id, "text": f.text} for f in item.fields],
            "questions": [{"id": q.id, "text": q.text} for q in item.questions],
            "template": item.template,
            "min_words": item.min_words,
            "max_words": item.max_words,
            "max_chars": item.max_chars,
        }
    base.update(_weights(unit))
    return base


def build_definition(form: AssessmentForm) -> dict[str, Any]:
    """The definition of a form, as plain JSON-able data."""
    by_part: dict[tuple[str, str], list[Unit]] = {}
    for unit in units(form):
        key = next(p.part_key for p in unit.section.parts if unit.item in p.items)
        by_part.setdefault((unit.section.key, key), []).append(unit)
    sections = []
    for section in form.sections:
        parts = []
        for part in section.parts:
            parts.append(
                {
                    "part_key": part.part_key,
                    "title": part.title,
                    "instructions": part.instructions,
                    "shared": _shared(part.shared),
                    "questions": [
                        _question(u)
                        for u in by_part.get((section.key, part.part_key), [])
                    ],
                }
            )
        sections.append(
            {
                "key": section.key,
                "title": section.title,
                "instructions": section.instructions,
                "weight": section.weight,
                "delivery": section.delivery,
                "max_points": section.max_points(),
                "parts": parts,
            }
        )
    return {
        "schema": SCHEMA,
        "form_id": form.form_id,
        "purpose": form.purpose,
        "language": form.language,
        "level": form.level,
        "title": form.title,
        "sections": sections,
    }


def canonical(value: Any) -> bytes:
    """The one byte form of JSON this module hashes and sizes."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def definition_hash(definition: Any) -> str:
    return hashlib.sha256(canonical(definition)).hexdigest()
