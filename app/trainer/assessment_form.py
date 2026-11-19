"""An assessment form as stored in a content pack (`assessments/<form_id>.json`).

This is the authoring format and it holds the keys, so it never leaves the
server. The API builds `AssessmentItem`s from it (see `app.assessment`), with
the options shuffled and renamed, and never includes a key.

Objective items score one point per key (a matching item scores one per pair).
Writing and Speaking tasks score 0 to 5 against a rubric of deterministic
checks (see `app.assessment.rubric`).
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.trainer.items import Option

PublicationState = Literal["draft", "reviewed", "published"]
SectionKey = Literal["listening", "reading", "use_of_english", "writing", "speaking"]
OBJECTIVE_SECTIONS: tuple[SectionKey, ...] = (
    "listening",
    "reading",
    "use_of_english",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Competence(_Frozen):
    """Where in the curriculum an item belongs, as the form states it, for
    example `M-I · 1.3.2 · la hora`. `modules` has several entries only for an
    item that spans modules (the matching task)."""

    raw: str
    modules: list[Literal["M1", "M2", "M3", "M4"]] = Field(min_length=1)
    lesson: str | None = None
    topic: str | None = None


class AudioClip(_Frozen):
    # Id in `audio_manifest.json`.
    audio: str
    transcript: str


class Shared(_Frozen):
    """What several items of a part share: a text, a picture sheet."""

    kind: Literal["passage", "image"]
    title: str | None = None
    text: str | None = None
    image_id: str | None = None
    alt_text: str | None = None
    # What the picture must show and say, for whoever draws it.
    design_note: str | None = None
    image_text: str | None = None


class _ItemBase(_Frozen):
    item_id: str
    prompt: str
    competence: Competence | None = None
    # The item's number in the client's document, for the reviewer only. Ids
    # are stable and independent of it; it is never served.
    source_number: int | None = None


class ChoiceAssessmentItem(_ItemBase):
    type: Literal["mcq", "listen_choose", "gap_choice"]
    options: list[Option] = Field(min_length=2)
    # Id of the right option, as the source marks it.
    key: str
    audio: AudioClip | None = None
    gap_label: str | None = None

    @model_validator(mode="after")
    def _key_is_an_option(self) -> "ChoiceAssessmentItem":
        if self.key not in {o.id for o in self.options}:
            raise ValueError(f"{self.item_id}: key is not one of the options")
        return self


class MatchAudioItem(_ItemBase):
    """Five clips to match with five places: five objective items."""

    type: Literal["match_audio"]
    clips: list[AudioClip] = Field(min_length=2)
    places: list[Option] = Field(min_length=2)
    # clip index (0-based, in `clips` order) -> place id
    key: list[tuple[int, str]] = Field(min_length=2)

    @property
    def points(self) -> int:
        return len(self.key)


class ProductiveItem(_ItemBase):
    """A Writing or Speaking task, scored 0 to 5 by a rubric."""

    type: Literal["form_fields", "template_text", "written_answers"]
    rubric: str
    fields: list[Option] = Field(default_factory=list)
    template: str | None = None
    min_words: int | None = None
    max_words: int | None = None
    # Characters of each answer that are kept and scored. Without it the shared
    # cap of 300 applies, which fits a sentence or two, not a 150-word letter.
    # Not for `template_text`, whose blanks are short by construction.
    max_chars: int | None = Field(default=None, ge=1, le=3000)
    questions: list[Option] = Field(default_factory=list)
    # What the document says about how it is evaluated, verbatim.
    source_rubric: str
    # Set when the written form is our equivalent of a spoken task.
    written_equivalent_of: str | None = None

    @model_validator(mode="after")
    def _max_chars_fits_the_type(self) -> "ProductiveItem":
        if self.max_chars is not None and self.type == "template_text":
            raise ValueError(f"{self.item_id}: max_chars is not for template_text")
        return self


AssessmentItemDef = Annotated[
    ChoiceAssessmentItem | MatchAudioItem | ProductiveItem,
    Field(discriminator="type"),
]


class Part(_Frozen):
    part_key: str
    title: str
    instructions: str
    shared: Shared | None = None
    items: list[AssessmentItemDef] = Field(min_length=1)


class Section(_Frozen):
    key: SectionKey
    title: str
    instructions: str | None = None
    # Percent of the total; the five sections add up to 100.
    weight: int = Field(ge=1, le=100)
    # Set when the section is not delivered the way the client's document
    # describes it (Speaking taken as typed answers). Authoring data only: it is
    # not served, so nothing claims to measure what the section does not.
    delivery: Literal["written_equivalent"] | None = None
    parts: list[Part] = Field(min_length=1)

    def items(self) -> list[AssessmentItemDef]:
        return [item for part in self.parts for item in part.items]

    def max_points(self) -> int:
        total = 0
        for item in self.items():
            if isinstance(item, ChoiceAssessmentItem):
                total += 1
            elif isinstance(item, MatchAudioItem):
                total += item.points
            else:
                total += 5
        return total


class AssessmentForm(_Frozen):
    form_id: str
    title: str
    language: str
    level: str
    # `check_in` (the entry diagnostic, Form A) or `final` (the exit exam). It is
    # the form's own identity: the settings decide which one a map uses.
    purpose: Literal["check_in", "final"] = "check_in"
    status: PublicationState = "draft"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    sections: list[Section] = Field(min_length=1)
    # Anything the importer noticed in the source and left as it was.
    source_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _weights_add_up(self) -> "AssessmentForm":
        if sum(s.weight for s in self.sections) != 100:
            raise ValueError("section weights must add up to 100")
        keys = [s.key for s in self.sections]
        if len(set(keys)) != len(keys):
            raise ValueError("a section appears twice")
        return self

    def section(self, key: str) -> Section:
        return next(s for s in self.sections if s.key == key)


__all__ = [
    "OBJECTIVE_SECTIONS",
    "AssessmentForm",
    "AssessmentItemDef",
    "AudioClip",
    "ChoiceAssessmentItem",
    "Competence",
    "MatchAudioItem",
    "Part",
    "ProductiveItem",
    "Section",
    "Shared",
]
