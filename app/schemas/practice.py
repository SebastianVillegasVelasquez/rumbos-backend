"""Wire shapes of the practice API (contract v2, section C). Snake_case like
the rest of the trainer contract (decision D12). Step payloads are free-form
objects whose fields depend on `type` (mcq, listen_choose, gap_fill, reorder,
match, short_answer, dialogue_turn); none carries an answer."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogActivity(_Out):
    id: str
    skill: str
    title: str
    estimated_minutes: int
    step_count: int
    role: str | None = None
    can_do: list[str]
    state: Literal["not_started", "in_progress", "finished"]
    best_percent: int | None = None


class CatalogLesson(_Out):
    id: str
    code: str
    title: str
    description: str
    vocabulary_count: int
    activities: list[CatalogActivity]


class CatalogModule(_Out):
    key: str
    title: str
    lessons: list[CatalogLesson]


class Catalog(_Out):
    language: str
    level: str
    title: str
    content_version: str
    modules: list[CatalogModule]


class SessionStart(_In):
    activity_id: str = Field(min_length=1, max_length=80)


class SessionView(_Out):
    session_id: uuid.UUID
    activity_id: str
    title: str
    status: Literal["in_progress", "finished"]
    kind: Literal["exercise", "dialogue"]
    steps: list[dict[str, Any]]
    summary: dict[str, Any] | None = None
    turn: int | None = None
    turns_total: int | None = None


class AnswerIn(_In):
    step_id: str = Field(min_length=1, max_length=80)
    # Chosen by the client for each try: a retry with the same id is answered
    # again and never counted twice.
    attempt_id: uuid.UUID
    response: dict[str, Any]


class Feedback(_Out):
    explanation: str | None = None
    closest_answer: str | None = None
    error_tags: list[str]


class AnswerOut(_Out):
    outcome: Literal["correct", "partial", "incorrect", "accepted"]
    earned: int
    total: int
    feedback: Feedback
    next: dict[str, Any] | None = None
    finished: bool


class FinishOut(_Out):
    session_id: uuid.UUID
    summary: dict[str, Any] | None


class LessonProgress(_Out):
    lesson_id: str
    activities_total: int
    activities_finished: int


class Progress(_Out):
    lessons: list[LessonProgress]
    words_saved: int
    words_due: int
    words_by_box: dict[str, int]


class VocabEntry(_Out):
    entry_id: str
    word: str
    translation: str
    lesson_id: str
    saved: bool
    box: int | None = None


class SavedWordOut(_Out):
    entry_id: str
    word: str
    translation: str
    lesson_id: str
    box: int
    due_at: datetime
    due: bool


class ReviewIn(_In):
    knew_it: bool


class ReportIn(_In):
    activity_id: str = Field(min_length=1, max_length=80)
    step_id: str | None = Field(default=None, max_length=80)
    text: str = Field(min_length=1, max_length=500)


class ReportAck(_Out):
    id: uuid.UUID


class ReportRow(_Out):
    id: uuid.UUID
    activity_id: str
    step_id: str | None
    content_version: str
    text: str
    status: str
    created_at: datetime


class Erased(_Out):
    sessions: int
    words: int
    reports: int
