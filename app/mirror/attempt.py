"""The attempt record the Moodle mirror stores, built when an attempt closes.
Pure.

It extends the result statement (`app.passback.statement`) with schema 2
claims: per question the student's answer, whether it was right and the points
awarded; the subtotal of each section; the exact percentage; and, for the
written tasks, the points the rubric gave and (only if the tenant allows it)
the text submitted. Everything is read from the closed attempt and the form it
was taken against and then frozen in the stored statement, so a resend is
byte-identical and a later change of the content never rewrites it.

The backend alone decides score and pass/fail: `correct`, `points_exact`,
`percent_exact` and `passed` are final and the mirror never recomputes them.

Size: at most `MAX_STATEMENT_BYTES` of JSON. Over it, the written text goes
first (`written_text_included: false`, `truncated: true`); an answer is never
dropped.
"""

import logging
from collections.abc import Mapping
from typing import Any

from app.assessment.engine import ScoredAttempt, clip_id, option_id
from app.mirror.definition import (
    Unit,
    canonical,
    units,
)
from app.trainer.assessment_form import (
    AssessmentForm,
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
)

log = logging.getLogger("rumbos.mirror")

SCHEMA = 2
MAX_STATEMENT_BYTES = 64 * 1024


def _choice_answer(
    form_id: str, item: ChoiceAssessmentItem, response: Mapping[str, Any]
) -> str | None:
    """The source id of the option the student chose (the one they saw was an
    opaque, per-form id), or None if unanswered."""
    chosen = response.get("option_id")
    for option in item.options:
        if option_id(form_id, item.item_id, option.id) == chosen:
            return option.id
    return None


def _pair_answer(form_id: str, unit: Unit, response: Mapping[str, Any]) -> str | None:
    assert isinstance(unit.item, MatchAudioItem) and unit.pair is not None
    wanted = clip_id(form_id, unit.item.item_id, unit.pair[0])
    given = {p[0]: p[1] for p in response.get("pairs", [])}
    return given.get(wanted)


def _entries(
    form: AssessmentForm,
    responses: Mapping[str, Mapping[str, Any]],
    scored: ScoredAttempt,
    include_text: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    answers: list[dict[str, Any]] = []
    written: list[dict[str, Any]] = []
    for unit in units(form):
        item = unit.item
        response = responses.get(item.item_id, {})
        if isinstance(item, ProductiveItem):
            values = response.get("values") if response else None
            written.append(
                {
                    "question_id": unit.question_id,
                    "points_exact": str(scored.item_points[item.item_id]),
                    "max_points": unit.max_points,
                    "text": dict(values) if include_text and values else None,
                }
            )
            continue
        if isinstance(item, ChoiceAssessmentItem):
            answer = _choice_answer(form.form_id, item, response)
            correct = answer is not None and answer == item.key
        else:
            answer = _pair_answer(form.form_id, unit, response)
            assert unit.pair is not None
            correct = answer is not None and answer == unit.pair[1]
        answers.append(
            {
                "question_id": unit.question_id,
                "answer": answer,
                "correct": correct,
                "points_exact": "1" if correct else "0",
                "max_points": unit.max_points,
            }
        )
    return answers, written


def attempt_claims(
    base: Mapping[str, Any],
    *,
    form: AssessmentForm,
    definition_hash: str,
    status: str,
    responses: Mapping[str, Mapping[str, Any]],
    scored: ScoredAttempt,
    include_written_text: bool,
    max_bytes: int = MAX_STATEMENT_BYTES,
) -> dict[str, Any]:
    """The claims of a schema 2 attempt statement: `base` (the schema 1 claims,
    unchanged) plus the record. `include_written_text` is the tenant's switch."""

    def build(include_text: bool, truncated: bool) -> dict[str, Any]:
        answers, written = _entries(form, responses, scored, include_text)
        return {
            **base,
            "schema": SCHEMA,
            "kind": "attempt",
            "status": status,
            "definition_hash": definition_hash,
            "percent_exact": str(scored.total.percent),
            "sections": [
                {
                    "key": s.key,
                    "points_exact": str(s.points),
                    "max": s.max,
                    "weight": s.weight,
                    "percent_exact": str(s.percent),
                }
                for s in scored.total.sections
            ],
            "answers": answers,
            "written": written,
            "written_text_included": include_text,
            "truncated": truncated,
        }

    claims = build(include_written_text, False)
    if len(canonical(claims)) <= max_bytes:
        return claims
    claims = build(False, True)
    if len(canonical(claims)) > max_bytes:
        # Unreachable for a form of this size (see the contract): never drop an
        # answer, say so loudly instead.
        log.error(
            "mirror_statement_over_cap attempt=%s bytes=%s",
            base.get("jti"),
            len(canonical(claims)),
        )
    return claims
