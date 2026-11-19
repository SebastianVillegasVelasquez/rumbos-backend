"""The exam as the student sees it, and as the server scores it. Pure.

`section_view` builds what the API serves: the items of one section with item
and option order shuffled by the attempt's seed, option ids made opaque, and
nothing that reveals a key (no key, no accepted answer, no rubric, no listening
transcript). `score_form` turns the saved responses into points per item, per
section and per module, with exact fractions.
"""

import hashlib
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from app.assessment.rubric import (
    Response,
    Rubric,
    blank_ids,
    fill_template,
    rubric_from,
    score,
)
from app.assessment.scoring import SectionScore, Total, module_percents, total_of
from app.assessment.vocabulary import course_vocabulary
from app.trainer.assessment_form import (
    AssessmentForm,
    AssessmentItemDef,
    ChoiceAssessmentItem,
    MatchAudioItem,
    Part,
    ProductiveItem,
)
from app.trainer.grading import InvalidResponseError
from app.trainer.language import MAX_FREE_TEXT_CHARS, LanguageRules
from app.trainer.pack import Pack

ZERO = Fraction(0)
AudioRef = Callable[[str], dict[str, Any]]


def _token(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:8]


def option_id(form_id: str, item_id: str, source_id: str) -> str:
    """An option id that says nothing about where the option came from."""
    return "o" + _token(form_id, item_id, source_id)


def _rng(seed: str, *parts: str) -> random.Random:
    digest = hashlib.sha256("|".join((seed, *parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _shuffled[T](values: list[T], rng: random.Random) -> list[T]:
    out = list(values)
    rng.shuffle(out)
    return out


@dataclass(frozen=True)
class FormRuntime:
    """A form with everything scoring it needs, built once per content version."""

    form: AssessmentForm
    rubrics: Mapping[str, Rubric]
    vocabulary: frozenset[str]

    def items(self) -> dict[str, AssessmentItemDef]:
        return {i.item_id: i for s in self.form.sections for i in s.items()}


_RUNTIMES: dict[tuple[str, str], FormRuntime] = {}


def runtime_for(pack: Pack, form_id: str, rules: LanguageRules) -> FormRuntime:
    key = (pack.content_version, form_id)
    found = _RUNTIMES.get(key)
    if found is None:
        form = pack.forms[form_id]
        found = FormRuntime(
            form,
            {k: rubric_from(v) for k, v in pack.rubrics.items()},
            course_vocabulary(form, pack.course_words, rules),
        )
        _RUNTIMES[key] = found
    return found


# --- what the student sees -----------------------------------------------------


def _item_view(
    form_id: str, item: AssessmentItemDef, seed: str, audio: AudioRef
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item_id": item.item_id,
        "type": item.type,
        "prompt": item.prompt,
    }
    if isinstance(item, ChoiceAssessmentItem):
        rng = _rng(seed, item.item_id)
        base["options"] = [
            {"option_id": option_id(form_id, item.item_id, o.id), "text": o.text}
            for o in _shuffled(item.options, rng)
        ]
        if item.audio:
            base["audio"] = audio(item.audio.audio)
        if item.gap_label:
            base["gap_label"] = item.gap_label
    elif isinstance(item, MatchAudioItem):
        rng = _rng(seed, item.item_id)
        clips = [
            {"id": "c" + _token(form_id, item.item_id, str(i)), "audio": audio(c.audio)}
            for i, c in enumerate(item.clips)
        ]
        base["clips"] = _shuffled(clips, rng)
        base["places"] = [
            {"id": p.id, "text": p.text} for p in _shuffled(item.places, rng)
        ]
    elif item.type == "form_fields":
        base["fields"] = [{"field_id": f.id, "label": f.text} for f in item.fields]
    elif item.type == "template_text":
        base["template"] = item.template
        base["min_words"] = item.min_words
        base["max_words"] = item.max_words
    else:
        base["questions"] = [{"q_id": q.id, "prompt": q.text} for q in item.questions]
    if isinstance(item, ProductiveItem) and item.type != "template_text":
        # Only when the form sets them, so an item without them is served as before.
        for name in ("min_words", "max_words", "max_chars"):
            if getattr(item, name) is not None:
                base[name] = getattr(item, name)
    return base


def _part_view(form_id: str, part: Part, seed: str, audio: AudioRef) -> dict[str, Any]:
    objective = [i for i in part.items if not isinstance(i, ProductiveItem)]
    productive = [i for i in part.items if isinstance(i, ProductiveItem)]
    ordered = _shuffled(objective, _rng(seed, "part", part.part_key)) + productive
    shared = None
    if part.shared:
        shared = {
            "kind": part.shared.kind,
            "title": part.shared.title,
            "text": part.shared.text,
            "image_id": part.shared.image_id,
            "alt_text": part.shared.alt_text,
        }
    return {
        "part_key": part.part_key,
        "instructions": part.instructions,
        "shared": shared,
        "items": [_item_view(form_id, i, seed, audio) for i in ordered],
    }


def section_view(
    form: AssessmentForm, section_key: str, seed: str, audio: AudioRef
) -> dict[str, Any]:
    section = form.section(section_key)
    return {
        "key": section.key,
        "title": section.title,
        "parts": [_part_view(form.form_id, p, seed, audio) for p in section.parts],
    }


def section_summaries(form: AssessmentForm) -> list[dict[str, Any]]:
    return [
        {
            "key": s.key,
            "title": s.title,
            "instructions": s.instructions or "",
            "item_count": len(s.items()),
        }
        for s in form.sections
    ]


# --- responses ------------------------------------------------------------------


def _values(
    response: Mapping[str, Any],
    allowed: list[str],
    limit: int = MAX_FREE_TEXT_CHARS,
) -> dict[str, str]:
    values = response.get("values")
    if not isinstance(values, Mapping) or not set(values) <= set(allowed):
        raise InvalidResponseError("unknown field")
    out: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(value, str):
            raise InvalidResponseError("a value must be text")
        out[key] = value[:limit]
    return out


def clean_response(
    form_id: str, item: AssessmentItemDef, response: Mapping[str, Any]
) -> dict[str, Any]:
    """The response in canonical form, or `InvalidResponseError`. Text is
    capped; ids must be ones the server served."""
    if isinstance(item, ChoiceAssessmentItem):
        chosen = response.get("option_id")
        valid = {option_id(form_id, item.item_id, o.id) for o in item.options}
        if not isinstance(chosen, str) or chosen not in valid:
            raise InvalidResponseError("unknown option")
        return {"option_id": chosen}
    if isinstance(item, MatchAudioItem):
        pairs = response.get("pairs")
        clips = {
            "c" + _token(form_id, item.item_id, str(i)) for i in range(len(item.clips))
        }
        places = {p.id for p in item.places}
        if not isinstance(pairs, list):
            raise InvalidResponseError("pairs must be a list")
        seen: set[str] = set()
        out: list[list[str]] = []
        for pair in pairs:
            ok = isinstance(pair, list | tuple) and len(pair) == 2
            if (
                not ok
                or pair[0] not in clips
                or pair[1] not in places
                or pair[0] in seen
            ):
                raise InvalidResponseError("invalid pair")
            seen.add(pair[0])
            out.append([pair[0], pair[1]])
        return {"pairs": out}
    limit = item.max_chars or MAX_FREE_TEXT_CHARS
    if item.type == "form_fields":
        return {"values": _values(response, [f.id for f in item.fields], limit)}
    if item.type == "template_text":
        return {"values": _values(response, blank_ids(item.template or ""))}
    return {"values": _values(response, [q.id for q in item.questions], limit)}


# --- scoring --------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredAttempt:
    total: Total
    item_points: Mapping[str, Fraction]
    modules: Mapping[str, Fraction]  # module -> percent


def _objective_points(
    form_id: str, item: AssessmentItemDef, response: Mapping[str, Any]
) -> Fraction:
    if isinstance(item, ChoiceAssessmentItem):
        right = option_id(form_id, item.item_id, item.key)
        return Fraction(int(response.get("option_id") == right))
    assert isinstance(item, MatchAudioItem)
    wanted = {
        "c" + _token(form_id, item.item_id, str(index)): place
        for index, place in item.key
    }
    given = {p[0]: p[1] for p in response.get("pairs", [])}
    return Fraction(
        sum(1 for clip, place in wanted.items() if given.get(clip) == place)
    )


def _productive_points(
    runtime: FormRuntime,
    item: ProductiveItem,
    response: Mapping[str, Any],
    rules: LanguageRules,
) -> Fraction:
    values: Mapping[str, str] = response.get("values", {})
    if item.type == "template_text":
        ids = blank_ids(item.template or "")
        text = fill_template(item.template or "", [values.get(i, "") for i in ids])
        shown = {i: values.get(i, "") for i in ids}
    else:
        order = [f.id for f in item.fields] or [q.id for q in item.questions]
        shown = {i: values.get(i, "") for i in order}
        text = " ".join(v for v in shown.values() if v)
    result = score(
        runtime.rubrics[item.rubric],
        Response(shown, text),
        runtime.vocabulary,
        rules,
        item.max_chars or MAX_FREE_TEXT_CHARS,
    )
    return result.points


def score_form(
    runtime: FormRuntime,
    responses: Mapping[str, Mapping[str, Any]],
    rules: LanguageRules,
) -> ScoredAttempt:
    form = runtime.form
    item_points: dict[str, Fraction] = {}
    sections: list[SectionScore] = []
    earned: dict[str, Fraction] = {}
    available: dict[str, Fraction] = {}
    for section in form.sections:
        points = ZERO
        for item in section.items():
            response = responses.get(item.item_id, {})
            if isinstance(item, ProductiveItem):
                got = (
                    _productive_points(runtime, item, response, rules)
                    if response
                    else ZERO
                )
            else:
                got = (
                    _objective_points(form.form_id, item, response)
                    if response
                    else ZERO
                )
                # Per-module map: single-module objective items only (decision D5).
                if isinstance(item, ChoiceAssessmentItem) and item.competence:
                    mods = item.competence.modules
                    if len(mods) == 1:
                        available[mods[0]] = available.get(mods[0], ZERO) + 1
                        earned[mods[0]] = earned.get(mods[0], ZERO) + got
            item_points[item.item_id] = got
            points += got
        sections.append(
            SectionScore(section.key, points, section.max_points(), section.weight)
        )
    return ScoredAttempt(
        total_of(sections), item_points, module_percents(earned, available)
    )
