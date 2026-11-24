"""The shapes of the statements the plugin receives, as models.

They exist to publish the contract as JSON Schema (`contracts/schemas/`) and to
fail a test when a statement the backend builds stops matching what was handed
to the plugin: the builders in `definition`, `attempt` and `statements` produce
plain dicts, and these models validate them with unknown keys refused.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Option(_Shape):
    id: str
    text: str


class Shared(_Shape):
    kind: Literal["passage", "image"]
    title: str | None
    text: str | None
    image_id: str | None
    alt_text: str | None


class Written(_Shape):
    """What a written task asks for. The rubric that scores it never travels."""

    fields: list[Option]
    questions: list[Option]
    template: str | None
    min_words: int | None
    max_words: int | None
    max_chars: int | None


class Question(_Shape):
    """One scorable unit: a choice item, one pair of a matching item, or a
    written task. `weight_points` is its exact share of the 100, so the mark of
    a question is `points_exact / max_points * weight_points`."""

    question_id: str
    item_id: str
    type: str
    prompt: str
    # Choice and matching questions: the options (places, for matching), the id
    # of the right one, the listening clip and the gap label.
    options: list[Option] | None = None
    correct: str | None = None
    audio_id: str | None = None
    gap_label: str | None = None
    clip: int | None = Field(default=None, description="Matching: clip number, from 1")
    written: Written | None = None
    max_points: int
    weight_points: str = Field(description="Exact fraction of 100, e.g. `5/3` or `2`")
    weight_points_decimal: str


class Part(_Shape):
    part_key: str
    title: str
    instructions: str
    shared: Shared | None
    questions: list[Question]


class Section(_Shape):
    key: str
    title: str
    instructions: str | None
    weight: int
    delivery: Literal["written_equivalent"] | None
    max_points: int
    parts: list[Part]


class Definition(_Shape):
    schema_: Literal[1] = Field(alias="schema")
    form_id: str
    purpose: Literal["check_in", "final"]
    language: str
    level: str
    title: str
    sections: list[Section]


class _Statement(_Shape):
    iss: str
    aud: Literal["moodle"]
    iat: int = Field(description="When it was created. There is no `exp`.")
    jti: str = Field(description="Accept each `jti` once; a repeat is a duplicate")
    tenant: str
    form_id: str
    content_version: str


class DefinitionStatement(_Statement):
    """Exam definition. `jti` is fixed by (tenant, form, `definition_hash`)."""

    kind: Literal["definition"]
    schema_: Literal[1] = Field(alias="schema")
    definition_hash: str = Field(description="sha256 of the canonical definition")
    definition: Definition


class ResultStatement(_Statement):
    """Result passback, schema 1 (no `schema` claim)."""

    sub: str
    course_id: int
    purpose: Literal["check_in", "final"]
    ordinal: int
    percent: str = Field(description="Two decimals, for display only")
    passed: bool
    completes_course: bool
    pass_percent: int
    pass_operator: Literal["gt", "gte"]
    finished_at: str


class SectionScore(_Shape):
    key: str
    points_exact: str
    max: int
    weight: int
    percent_exact: str


class Answer(_Shape):
    question_id: str
    answer: str | None = Field(description="Source option id, or null if unanswered")
    correct: bool
    points_exact: str
    max_points: int


class WrittenScore(_Shape):
    question_id: str
    points_exact: str = Field(description="What the backend's rubric awarded")
    max_points: int
    text: dict[str, str] | None = Field(
        description="Field or question id to text; null unless `written_text_included`"
    )


class AttemptStatement(ResultStatement):
    """Attempt record, schema 2: the result claims plus the record. The backend
    alone decides `correct`, `points_exact`, `percent_exact` and `passed`."""

    kind: Literal["attempt"]
    schema_: Literal[2] = Field(alias="schema")
    status: Literal["submitted", "expired"]
    definition_hash: str
    percent_exact: str = Field(description="Exact fraction of 100, e.g. `401/5`")
    sections: list[SectionScore]
    answers: list[Answer]
    written: list[WrittenScore]
    written_text_included: bool
    truncated: bool = Field(
        description="True only if the 64 KB cap forced the written text out"
    )


SCHEMAS: dict[str, type[BaseModel]] = {
    "result-statement": ResultStatement,
    "definition-statement": DefinitionStatement,
    "attempt-statement": AttemptStatement,
}
