"""The pack linter: content rules that the typed models cannot express.

Errors stop CI; warnings are for the reviewer (drafts may still lack audio
files, published content may not). Pure over a loaded `Pack`.
"""

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.trainer.assessment_form import (
    ChoiceAssessmentItem,
    MatchAudioItem,
    ProductiveItem,
)
from app.trainer.items import (
    ChoiceItem,
    Dialogue,
    GapFillItem,
    MatchItem,
    ReorderItem,
    ShortAnswerItem,
)
from app.trainer.language import ENGLISH, LanguageRules
from app.trainer.pack import SKILLS, Activity, Pack

_UNFINISHED = re.compile(
    r"\b(todo|fixme|tbd|xxx|lorem ipsum|placeholder)\b", re.IGNORECASE
)
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_MIN_DUPLICATE_CHARS = 12


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [f"ERROR   {e}" for e in self.errors]
        lines += [f"warning {w}" for w in self.warnings]
        lines.append("")
        lines.append("Coverage (activities per lesson and skill):")
        for lesson, by_skill in sorted(self.coverage.items()):
            cells = " ".join(f"{s}={by_skill.get(s, 0)}" for s in SKILLS)
            lines.append(f"  {lesson:<4} {cells}")
        lines.append(f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        return "\n".join(lines)


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, BaseModel):
        return _strings(value.model_dump())
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _practice_stems(activity: Activity) -> list[str]:
    stems: list[str] = []
    for item in activity.items:
        stems.append(item.prompt)
        if isinstance(item, GapFillItem):
            stems.append(item.template)
    if activity.dialogue:
        stems += [n.partner_line for n in activity.dialogue.nodes]
    return stems


def _assessment_stems(pack: Pack) -> dict[str, str]:
    """Normalized stem -> where it is in the exam."""
    found: dict[str, str] = {}
    for form in pack.forms.values():
        for section in form.sections:
            for part in section.parts:
                if part.shared and part.shared.text:
                    key = ENGLISH.normalize(part.shared.text)
                    found[key] = f"{form.form_id} passage"
                for item in part.items:
                    where = f"{form.form_id} {item.item_id}"
                    found[ENGLISH.normalize(item.prompt)] = where
                    if isinstance(item, ChoiceAssessmentItem) and item.audio:
                        found[ENGLISH.normalize(item.audio.transcript)] = where
                    if isinstance(item, MatchAudioItem):
                        for clip in item.clips:
                            found[ENGLISH.normalize(clip.transcript)] = where
    return found


def _check_dialogue(name: str, dialogue: Dialogue, report: Report) -> None:
    ids = [n.id for n in dialogue.nodes]
    if len(set(ids)) != len(ids):
        report.errors.append(f"{name}: duplicate dialogue node ids")
    known = set(ids)
    if dialogue.start not in known:
        report.errors.append(f"{name}: start node {dialogue.start!r} does not exist")
        return
    edges: dict[str, list[str | None]] = {}
    for node in dialogue.nodes:
        if node.mode == "choose":
            if len(node.options) < 2:
                report.errors.append(
                    f"{name}/{node.id}: a choose turn needs 2+ options"
                )
            edges[node.id] = [o.next for o in node.options]
        else:
            if not node.accept or not node.model_reply:
                report.errors.append(
                    f"{name}/{node.id}: a typed turn needs accepted replies "
                    "and a model reply"
                )
            edges[node.id] = [a.next for a in node.accept]
        for target in edges[node.id]:
            if target is not None and target not in known:
                report.errors.append(
                    f"{name}/{node.id}: next {target!r} does not exist"
                )
    reachable: set[str] = set()
    stack = [dialogue.start]
    while stack:
        current = stack.pop()
        if current in reachable or current not in edges:
            continue
        reachable.add(current)
        stack += [t for t in edges[current] if t is not None]
    for orphan in sorted(known - reachable):
        report.errors.append(f"{name}/{orphan}: unreachable dialogue node")
    # Every reachable node must be able to reach an end (a `None` next).
    can_end = {n for n, outs in edges.items() if None in outs}
    changed = True
    while changed:
        changed = False
        for node_id, outs in edges.items():
            if node_id not in can_end and any(t in can_end for t in outs if t):
                can_end.add(node_id)
                changed = True
    for node_id in sorted(reachable - can_end):
        report.errors.append(f"{name}/{node_id}: dialogue dead end (cannot end)")


def _check_item(name: str, item: object, rules: LanguageRules, report: Report) -> None:
    def distinct(label: str, texts: list[str]) -> None:
        normalized = [rules.normalize(t) for t in texts]
        if len(set(normalized)) != len(normalized):
            report.errors.append(f"{name}: {label} collide after normalizing")

    if isinstance(item, ChoiceItem):
        distinct("options", [o.text for o in item.options])
        ids = {o.id for o in item.options}
        if item.correct not in ids or not set(item.error_tags) <= ids:
            report.errors.append(f"{name}: correct or error_tags name unknown options")
    elif isinstance(item, GapFillItem):
        found = set(_PLACEHOLDER.findall(item.template))
        if found != {g.gap_id for g in item.gaps}:
            report.errors.append(f"{name}: template placeholders and gaps differ")
        for gap in item.gaps:
            if gap.options is not None:
                if gap.correct not in {o.id for o in gap.options}:
                    report.errors.append(
                        f"{name}: gap {gap.gap_id} has no correct option"
                    )
                distinct(f"gap {gap.gap_id} options", [o.text for o in gap.options])
            elif not gap.accepted:
                report.errors.append(f"{name}: typed gap {gap.gap_id} accepts nothing")
            else:
                distinct(f"gap {gap.gap_id} accepted answers", gap.accepted)
    elif isinstance(item, ReorderItem):
        token_ids = [t.id for t in item.tokens]
        bad = any(sorted(a) != sorted(token_ids) for a in item.alternatives)
        if len(set(token_ids)) != len(token_ids) or bad:
            report.errors.append(f"{name}: tokens or alternatives are inconsistent")
    elif isinstance(item, MatchItem):
        left, right = {o.id for o in item.left}, {o.id for o in item.right}
        if not all(a in left and b in right for a, b in item.pairs):
            report.errors.append(f"{name}: a pair names an unknown id")
    elif isinstance(item, ShortAnswerItem):
        tokens = [" ".join(rules.tokenize(a)) for a in item.accepted]
        if len(set(tokens)) != len(tokens):
            report.errors.append(f"{name}: accepted answers collide after normalizing")


def lint(pack: Pack, rules: LanguageRules = ENGLISH) -> Report:
    report = Report()
    lessons = {lesson.id for lesson in pack.curriculum.lessons}
    modules = {m.key for m in pack.curriculum.modules}
    for lesson in pack.curriculum.lessons:
        if lesson.module not in modules:
            report.errors.append(f"lesson {lesson.id}: unknown module {lesson.module}")
    exam = _assessment_stems(pack)

    def check_audio(owner: str, audio_id: str | None, published: bool) -> None:
        if audio_id is None:
            return
        entry = pack.audio.get(audio_id)
        if entry is None:
            report.errors.append(f"{owner}: audio {audio_id!r} is not in the manifest")
            return
        problems = []
        if not (pack.root / entry.file).is_file():
            problems.append(f"audio file {entry.file} is missing")
        if entry.duration_s is None:
            problems.append(f"audio {audio_id} has no duration")
        target = report.errors if published else report.warnings
        target.extend(f"{owner}: {p}" for p in problems)

    for activity in pack.activities.values():
        name = f"activity {activity.id}"
        published = activity.status == "published"
        if activity.lesson_id not in lessons:
            report.errors.append(f"{name}: unknown lesson {activity.lesson_id}")
        by_skill = report.coverage.setdefault(activity.lesson_id, {})
        by_skill[activity.skill] = by_skill.get(activity.skill, 0) + 1
        if published and not (activity.reviewed_by and activity.reviewed_at):
            report.errors.append(f"{name}: published without reviewed_by/reviewed_at")
        if activity.skill == "dialogue":
            if activity.dialogue is None:
                report.errors.append(f"{name}: a dialogue activity has no dialogue")
            else:
                _check_dialogue(name, activity.dialogue, report)
                for node in activity.dialogue.nodes:
                    check_audio(f"{name}/{node.id}", node.audio, published)
        elif not activity.items:
            report.errors.append(f"{name}: no items")
        for item in activity.items:
            iname = f"{name}/{item.id}"
            if not (item.explanation and item.explanation.strip()):
                report.errors.append(f"{iname}: graded item without an explanation")
            check_audio(iname, item.audio, published)
            listening_ok = isinstance(item, ChoiceItem) and item.audio
            if activity.skill == "listening" and not listening_ok:
                report.errors.append(
                    f"{iname}: a listening item needs audio and options"
                )
            _check_item(iname, item, rules, report)
        for stem in _practice_stems(activity):
            key = rules.normalize(stem)
            if len(key) >= _MIN_DUPLICATE_CHARS and key in exam:
                report.errors.append(
                    f"{name}: repeats an exam item ({exam[key]}): {stem!r}"
                )
        for text in _strings(activity.model_dump(exclude={"flags"})):
            if _UNFINISHED.search(text):
                report.errors.append(f"{name}: unfinished text {text[:40]!r}")
                break
    seen_words: set[tuple[str, str]] = set()
    for entry in pack.vocabulary.values():
        if entry.lesson_id not in lessons:
            report.errors.append(
                f"vocabulary {entry.id}: unknown lesson {entry.lesson_id}"
            )
        word_key = (entry.lesson_id, rules.normalize(entry.word))
        if word_key in seen_words:
            report.errors.append(f"vocabulary {entry.id}: word repeated in lesson")
        seen_words.add(word_key)
    for form in pack.forms.values():
        published = form.status == "published"
        if published and not (form.reviewed_by and form.reviewed_at):
            report.errors.append(f"form {form.form_id}: published without reviewer")
        for section in form.sections:
            for form_item in section.items():
                where = f"form {form.form_id} {form_item.item_id}"
                if form_item.competence and form_item.competence.lesson:
                    code = ".".join(form_item.competence.lesson.split(".")[:2])
                    if code not in lessons:
                        report.errors.append(
                            f"{where}: competence lesson {code} unknown"
                        )
                if isinstance(form_item, ChoiceAssessmentItem):
                    check_audio(
                        where,
                        form_item.audio.audio if form_item.audio else None,
                        published,
                    )
                elif isinstance(form_item, MatchAudioItem):
                    for clip in form_item.clips:
                        check_audio(where, clip.audio, published)
                elif (
                    isinstance(form_item, ProductiveItem)
                    and form_item.rubric not in pack.rubrics
                ):
                    report.errors.append(f"{where}: unknown rubric {form_item.rubric}")
        for text in _strings(form.model_dump(exclude={"source_notes"})):
            if _UNFINISHED.search(text):
                report.errors.append(
                    f"form {form.form_id}: unfinished text {text[:40]!r}"
                )
                break
    for audio in pack.audio.values():
        if not audio.transcript.strip():
            report.errors.append(f"audio {audio.id}: no transcript")
    for lesson_id in sorted(lessons):
        by_skill = report.coverage.get(lesson_id, {})
        missing = [s for s in SKILLS if s not in by_skill]
        if missing and pack.activities:
            report.warnings.append(
                f"lesson {lesson_id}: no activity for {', '.join(missing)}"
            )
    return report
