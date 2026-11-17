"""Content packs: the typed files of `content/trainer/<language>/<level>/` and
the immutable catalog built from them at start-up.

A pack that does not load is refused whole (`PackError`, with every problem
listed); the app never serves half-loaded content. `content_version` is a hash
of every file, so a content change is visible to clients and caches.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.trainer.assessment_form import AssessmentForm, PublicationState
from app.trainer.items import Dialogue, GradedItem

Skill = Literal["dialogue", "listening", "grammar", "writing", "vocabulary"]
SKILLS: tuple[Skill, ...] = (
    "dialogue",
    "listening",
    "grammar",
    "writing",
    "vocabulary",
)


class PackError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LanguageInfo(_Frozen):
    code: str
    name: str


class PackMeta(_Frozen):
    language: LanguageInfo
    level: str
    title: str
    status: PublicationState = "draft"


class Module(_Frozen):
    key: str
    title: str


class Lesson(_Frozen):
    id: str
    module: str
    code: str
    title: str
    description: str


class Curriculum(_Frozen):
    modules: list[Module]
    lessons: list[Lesson]


class Activity(_Frozen):
    id: str
    lesson_id: str
    skill: Skill
    title: str
    estimated_minutes: int = Field(ge=1, le=60)
    status: PublicationState = "draft"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    role: str | None = None
    can_do: list[str] = Field(default_factory=list)
    items: list[GradedItem] = Field(default_factory=list)
    dialogue: Dialogue | None = None
    # Anything the author doubts, for the reviewer.
    flags: list[str] = Field(default_factory=list)

    @property
    def step_count(self) -> int:
        return len(self.dialogue.nodes) if self.dialogue else len(self.items)


class VocabEntry(_Frozen):
    id: str
    lesson_id: str
    word: str
    translation: str


class Voice(_Frozen):
    voice: str
    text: str


class AudioEntry(_Frozen):
    id: str
    text: str
    # Two-speaker audio: one entry per turn, each in its own voice.
    voices: list[Voice] = Field(default_factory=list)
    file: str
    duration_s: float | None = Field(default=None, gt=0)
    transcript: str


class AudioManifest(_Frozen):
    entries: list[AudioEntry] = Field(default_factory=list)


@dataclass(frozen=True)
class Pack:
    root: Path
    meta: PackMeta
    curriculum: Curriculum
    activities: Mapping[str, Activity]
    vocabulary: Mapping[str, VocabEntry]
    forms: Mapping[str, AssessmentForm]
    audio: Mapping[str, AudioEntry]
    rubrics: Mapping[str, Any]
    course_words: tuple[str, ...]
    content_version: str

    def served_activities(self, allow_drafts: bool) -> list[Activity]:
        return [
            a
            for a in self.activities.values()
            if allow_drafts or a.status == "published"
        ]

    def served_form(self, form_id: str, allow_drafts: bool) -> AssessmentForm | None:
        form = self.forms.get(form_id)
        if form is None or (form.status != "published" and not allow_drafts):
            return None
        return form


def _read(path: Path, model: type[BaseModel], problems: list[str]) -> Any:
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        problems.append(f"{path.name}: {first}")
        return None


def _hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def load_pack(root: Path) -> Pack:
    """Loads and cross-checks a pack, or raises `PackError` listing everything
    wrong. Only structure is checked here; content rules are the linter's."""
    problems: list[str] = []
    meta = _read(root / "pack.json", PackMeta, problems)
    curriculum = _read(root / "lessons.json", Curriculum, problems)
    manifest = (
        _read(root / "audio_manifest.json", AudioManifest, problems)
        if (root / "audio_manifest.json").exists()
        else AudioManifest()
    )
    vocabulary: list[VocabEntry] = []
    vocab_path = root / "vocabulary.json"
    if vocab_path.exists():
        try:
            raw = json.loads(vocab_path.read_text(encoding="utf-8"))
            vocabulary = [VocabEntry.model_validate(v) for v in raw["entries"]]
        except (ValueError, KeyError, ValidationError) as exc:
            problems.append(f"vocabulary.json: {str(exc).splitlines()[0]}")
    activities: list[Activity] = []
    for path in sorted((root / "activities").glob("*.json")):
        activity = _read(path, Activity, problems)
        if activity:
            activities.append(activity)
    forms: list[AssessmentForm] = []
    for path in sorted((root / "assessments").glob("*.json")):
        form = _read(path, AssessmentForm, problems)
        if form:
            if form.form_id != path.stem:
                problems.append(f"{path.name}: form_id must equal the file name")
            forms.append(form)
    rubrics: dict[str, Any] = {}
    words: tuple[str, ...] = ()
    try:
        rubric_doc = json.loads((root / "rubrics.json").read_text(encoding="utf-8"))
        rubrics = {r["id"]: r for r in rubric_doc["rubrics"]}
        words_doc = json.loads((root / "course_words.json").read_text(encoding="utf-8"))
        words = tuple(words_doc["words"])
    except (OSError, ValueError, KeyError) as exc:
        problems.append(f"rubrics.json or course_words.json: {exc}")
    for label, ids in (
        ("activity", [a.id for a in activities]),
        ("vocabulary entry", [v.id for v in vocabulary]),
        ("audio entry", [e.id for e in (manifest.entries if manifest else [])]),
        ("form", [f.form_id for f in forms]),
    ):
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            problems.append(f"duplicate {label} id {dup!r}")
    if problems or meta is None or curriculum is None or manifest is None:
        raise PackError(problems or ["pack is incomplete"])
    return Pack(
        root=root,
        meta=meta,
        curriculum=curriculum,
        activities=MappingProxyType({a.id: a for a in activities}),
        vocabulary=MappingProxyType({v.id: v for v in vocabulary}),
        forms=MappingProxyType({f.form_id: f for f in forms}),
        audio=MappingProxyType({e.id: e for e in manifest.entries}),
        rubrics=MappingProxyType(rubrics),
        course_words=words,
        content_version=_hash(root),
    )
