"""Wire shapes of the assessment API (contract v2, `/api/v1/trainer`).

The contract names its fields in snake_case, so these models do not use the
camelCase alias of the rest of the API (decision D12). Responses never carry
a key, an accepted answer, a rubric or a per-item verdict.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Purpose = Literal["check_in", "final"]
Band = Literal["emerging", "developing", "consolidating", "achieved"]
SyncState = Literal["not_required", "pending", "synced", "failed"]
ItemState = Literal["available", "in_progress", "exhausted", "locked", "passed"]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Policy(_Out):
    pass_percent: int
    pass_operator: Literal["gt", "gte"]
    time_limit_min: int


class InProgress(_Out):
    attempt_id: uuid.UUID
    deadline_at: datetime


class Attempts(_Out):
    used: int
    max: int


class Best(_Out):
    percent: str
    passed: bool
    finished_at: datetime


class AssessmentListItem(_Out):
    purpose: Purpose
    title: str
    # State of the exam form, shown only to an editor in preview.
    publication_state: Literal["draft", "reviewed", "published"] | None = None
    section_count: int
    item_count: int
    attempts: Attempts
    state: ItemState
    locked_reason: str | None = None
    in_progress: InProgress | None = None
    best: Best | None = None
    # The student has already passed, in this exam or (with the exemption on)
    # in the other one, so a client must not force another attempt. Never true
    # for an editor in preview. The pass rule and `state` do not change.
    already_passed: bool = False


class AssessmentFormEntry(_Out):
    """An exam form the deployed content has, whatever its state: what an editor
    picks from when configuring `assessments.check_in.form_id`."""

    id: str
    title: str
    # What the form is, so an editor never has to guess it from the title.
    language: str
    level: str
    purpose: Literal["check_in", "final"]
    publication_state: Literal["draft", "reviewed", "published"]


class AssessmentList(_Out):
    enabled: bool
    preview: bool = False
    policy: Policy | None = None
    items: list[AssessmentListItem] = Field(default_factory=list)


class SectionSummary(_Out):
    key: str
    title: str
    instructions: str
    item_count: int


class AttemptStarted(_Out):
    attempt_id: uuid.UUID
    started_at: datetime
    deadline_at: datetime
    server_now: datetime
    sections: list[SectionSummary]


class AudioRef(_Out):
    url: str
    duration_s: float | None = None


class Option(_Out):
    option_id: str
    text: str


class Clip(_Out):
    id: str
    audio: AudioRef


class Place(_Out):
    id: str
    text: str


class Field_(_Out):
    field_id: str
    label: str


class Question(_Out):
    q_id: str
    prompt: str


class AssessmentItem(_Out):
    """One item. Which fields are present depends on `type`:
    mcq, listen_choose, gap_choice: options (listen_choose: audio;
    gap_choice: gap_label); match_audio: clips, places; form_fields: fields;
    template_text: template, min_words, max_words; written_answers: questions."""

    item_id: str
    type: Literal[
        "mcq",
        "listen_choose",
        "gap_choice",
        "match_audio",
        "form_fields",
        "template_text",
        "written_answers",
    ]
    prompt: str
    options: list[Option] | None = None
    audio: AudioRef | None = None
    gap_label: str | None = None
    clips: list[Clip] | None = None
    places: list[Place] | None = None
    fields: list[Field_] | None = None
    template: str | None = None
    min_words: int | None = None
    max_words: int | None = None
    questions: list[Question] | None = None


class Shared(_Out):
    kind: Literal["passage", "image"]
    title: str | None = None
    text: str | None = None
    image_id: str | None = None
    alt_text: str | None = None


class SectionPart(_Out):
    part_key: str
    instructions: str
    shared: Shared | None = None
    items: list[AssessmentItem]


class SectionRead(_Out):
    key: str
    title: str
    deadline_at: datetime
    server_now: datetime
    parts: list[SectionPart]
    saved: dict[str, Any]


class AnswerIn(_In):
    item_id: str = Field(min_length=1, max_length=80)
    response: dict[str, Any]


class SaveAnswers(_In):
    answers: list[AnswerIn] = Field(max_length=80)


class SaveAck(_Out):
    saved_at: datetime
    server_now: datetime
    deadline_at: datetime


class SectionResult(_Out):
    key: str
    points: str
    max: int
    percent: str
    weight: int


class ModuleResult(_Out):
    module: str
    percent: str


class Sync(_Out):
    state: SyncState


class Result(_Out):
    status: Literal["submitted", "expired"]
    percent: str
    passed: bool
    band: Band
    by_section: list[SectionResult]
    by_module: list[ModuleResult]
    attempts: Attempts
    gain: str | None = None
    sync: Sync
