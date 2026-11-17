"""The definitions of practice exercises, as written in a content pack.

These hold the answers, so they never leave the server: the API turns each one
into a `Step` that carries only what a student may see. Every definition is
immutable and strictly typed; a pack with an unknown field is refused.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorTag(_Frozen):
    """A named kind of mistake with the sentence that explains it."""

    tag: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=300)


class Option(_Frozen):
    id: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=200)


class _ItemBase(_Frozen):
    id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=400)
    hint: str | None = None
    # Id of an entry of `audio_manifest.json`.
    audio: str | None = None
    # Why the answer is what it is; required for every graded item.
    explanation: str | None = None


class ChoiceItem(_ItemBase):
    """`mcq` and `listen_choose`: one option is right."""

    type: Literal["mcq", "listen_choose"]
    options: list[Option] = Field(min_length=2)
    correct: str
    # Option id -> the mistake someone who picks it is probably making.
    error_tags: dict[str, ErrorTag] = Field(default_factory=dict)


class Gap(_Frozen):
    gap_id: str = Field(min_length=1, max_length=20)
    # With options the student picks one (`correct` is an option id); without,
    # the student types and `accepted` lists the texts that count.
    options: list[Option] | None = None
    correct: str | None = None
    accepted: list[str] = Field(default_factory=list)
    error_tag: ErrorTag | None = None


class GapFillItem(_ItemBase):
    type: Literal["gap_fill"]
    template: str = Field(min_length=1, max_length=400)
    gaps: list[Gap] = Field(min_length=1)


class ReorderItem(_ItemBase):
    type: Literal["reorder"]
    # Listed in the correct order; the API shuffles them per session.
    tokens: list[Option] = Field(min_length=2)
    # Other orders that are also right, as lists of token ids.
    alternatives: list[list[str]] = Field(default_factory=list)
    error_tag: ErrorTag | None = None


class MatchItem(_ItemBase):
    type: Literal["match"]
    left: list[Option] = Field(min_length=2)
    right: list[Option] = Field(min_length=2)
    pairs: list[tuple[str, str]] = Field(min_length=2)
    error_tag: ErrorTag | None = None


class ShortAnswerItem(_ItemBase):
    type: Literal["short_answer"]
    accepted: list[str] = Field(min_length=1)
    max_length: int = Field(default=60, ge=1, le=300)
    error_tag: ErrorTag | None = None


class DialogueChoice(_Frozen):
    id: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=200)
    # Where the conversation goes when this is chosen; None ends it.
    next: str | None = None


class DialogueAccept(_Frozen):
    """A typed reply is accepted when it has every word of one of these sets."""

    all_of: list[str] = Field(min_length=1)
    next: str | None = None


class DialogueNode(_Frozen):
    id: str = Field(min_length=1, max_length=40)
    partner_line: str = Field(min_length=1, max_length=300)
    translation: str | None = None
    audio: str | None = None
    mode: Literal["choose", "type"]
    options: list[DialogueChoice] = Field(default_factory=list)
    accept: list[DialogueAccept] = Field(default_factory=list)
    starters: list[str] = Field(default_factory=list)
    word_bank: list[str] = Field(default_factory=list)
    # What to show as the model reply when a typed one is not accepted.
    model_reply: str | None = None


class Dialogue(_Frozen):
    """A scripted conversation: a graph of turns, not graded, never AI."""

    start: str
    nodes: list[DialogueNode] = Field(min_length=1)

    def node(self, node_id: str) -> DialogueNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)


GradedItem = Annotated[
    ChoiceItem | GapFillItem | ReorderItem | MatchItem | ShortAnswerItem,
    Field(discriminator="type"),
]
