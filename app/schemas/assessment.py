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
    # True for an editor's trial run: never counted, reported or graded.
    preview: bool = False
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
    template_text: template, min_words, max_words; written_answers: questions
    (and, if the form sets them, min_words, max_words, max_chars)."""

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
    # form_fields and written_answers: how many characters of an answer are kept
    # (absent: the default cap of 300). min_words/max_words are also sent for
    # them when the form states a length, as a hint for the student.
    max_chars: int | None = None
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
    # True for an editor's trial run: nothing of it is recorded as a result.
    preview: bool = False
    percent: str
    passed: bool
    pass_percent: int = Field(
        description="The pass mark this attempt was graded with (frozen when it "
        "started). For display: read `passed`, never compare `percent` with it."
    )
    pass_operator: Literal["gt", "gte"] = Field(
        description="`gt`: the score must be above `pass_percent`; `gte`: at "
        "least it. Decided on the exact score; `percent` is rounded for display."
    )
    band: Band
    by_section: list[SectionResult]
    by_module: list[ModuleResult]
    attempts: Attempts
    gain: str | None = None
    sync: Sync


ClosingReason = Literal[
    "not_enabled",
    "no_final_step",
    "form_unavailable",
    "already_passed",
    "no_attempts_left",
    "scorm_incomplete",
    "progress_unavailable",
    "user_unlinked",
]


class ClosingProgress(_Out):
    """How much of the map's required SCORM content the student has finished.
    Counts only: which activities is not told."""

    completed: int
    total: int


CheckinStatus = Literal["not_taken", "passed", "not_passed"]
FinalStatus = Literal[
    "required", "not_required_passed_checkin", "passed", "exhausted", "locked"
]


class CheckinOutcome(_Out):
    """How the student's check-in went. `not_taken` also covers an attempt still
    in progress. `percent` is the score of the attempt that decided it (the
    passing one, else the best), as the exam reports it; `pass_percent` and
    `pass_operator` are the rule that attempt was graded with (the map's current
    rule while nothing was sat). The rule is `percent` `pass_operator`
    `pass_percent`: with `gt`, exactly the threshold does not pass. It is decided
    on the exact score and `percent` is rounded to two decimals for display, so
    a client must show `status` and never recompute it from `percent`."""

    status: CheckinStatus
    percent: str | None
    pass_percent: int
    pass_operator: Literal["gt", "gte"]
    attempts: Attempts


class FinalOutcome(_Out):
    """The final exam's status for this student: `required` (they may sit it now),
    `locked` (they must, but not yet: `reason` says why, as for `available`),
    `not_required_passed_checkin` (a passed check-in exempts them and the
    closing flow is shown without the exam step), `passed`, or `exhausted` (every
    attempt used without passing). `attempts` is null when the map has no final
    exam.

    `percent`, `pass_percent` and `pass_operator` are those of the final attempt
    that decides: the passing one, else the one with the best score (not
    necessarily the latest; one attempt's own are in its result). The rule is
    the one that attempt was graded with. With no final sat, `percent` is null
    and the rule is the map's current one. All three are null when the map has
    no final exam. Read `status`, never compare `percent`."""

    status: FinalStatus
    reason: ClosingReason | None
    attempts: Attempts | None
    percent: str | None
    pass_percent: int | None
    pass_operator: Literal["gt", "gte"] | None


class ScormGate(_Out):
    """Whether the SCORM condition of the gate is met. `satisfied` is true with
    `reason: passed_checkin` when a passed check-in met it without the content
    being done (the plugin then marks the content complete from the signed
    statement), false while content is missing, and null when it was not checked
    or could not be read. With `CLOSING_GATE_MODE=observe` it is always
    `{satisfied: null, reason: not_checked}`, a passed check-in included: the
    server does not know whether the content is done."""

    satisfied: bool | None
    reason: Literal["passed_checkin", "not_checked"] | None


class ClosingOutcome(_Out):
    """The student's journey through the two exams, read from their attempts
    (nothing about it is stored: D29)."""

    checkin: CheckinOutcome | None
    final: FinalOutcome
    scorm_gate: ScormGate


class ClosingStatus(_Out):
    """Whether the closing flow's final exam may be started now, by the same
    rule the start call enforces (see `app.closing.gate`)."""

    # True only with `CLOSING_GATE_MODE=enforce`. False with `off` and with
    # `observe`: the content is not checked and `progress` is not computed. It
    # does not say whether `outcome` is there.
    enforced: bool
    # True for an editor in preview: the SCORM and attempt conditions are
    # skipped and an attempt is a trial run (`preview: true`).
    preview: bool
    available: bool
    reason: ClosingReason | None = None
    progress: ClosingProgress | None = None
    final: AssessmentListItem | None = Field(
        default=None,
        description="The final exam's list item, as `/trainer/assessments` shapes "
        "it; null when the map has no final exam. Use it for the attempt in "
        "progress and the best result. Do not use `final.state` to decide the "
        "journey: it only describes this exam's own attempts and ignores the "
        "SCORM condition and the check-in exemption (it can say `available` "
        "while the exam is locked or not required). Read `outcome.final.status`.",
    )
    # The check-in outcome and the final's status, to drive the journey. Present
    # with `CLOSING_GATE_MODE=enforce` and `observe`; with `off` the key is
    # absent, so the answer is exactly what it was before. `checkin` is null when
    # the map has no check-in.
    outcome: ClosingOutcome | None = None
