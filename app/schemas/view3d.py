"""The 3D view of a map: which scene, and which bubble sits in which zone.

Two families of models share the wire shape. `View3d` and friends are what is
READ (from the draft, or from a frozen snapshot): plain, so a snapshot published
under older limits can always be shown. `View3dUpdate` and friends are what is
WRITTEN: they clean the text and enforce the limits.
"""

import unicodedata
import uuid
from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator, model_serializer
from pydantic.functional_validators import AfterValidator
from pydantic_core.core_schema import SerializerFunctionWrapHandler

from app.schemas.base import ApiModel

TITLE_MAX_LENGTH = 120
DESCRIPTION_MAX_LENGTH = 600
SCENE_MAX_LENGTH = 60
# Far above any scene's zone count; only keeps a request from being absurd.
MAX_BINDINGS = 100


class Scene(ApiModel):
    id: str
    zone_count: int


class SceneList(ApiModel):
    items: list[Scene]


# --- reading ------------------------------------------------------------------


class LocalizedText(ApiModel):
    """Per-language text. A language that was not given is absent from the JSON
    (not `null`), as the contract's `{ es?: string, en?: string }` says."""

    es: str | None = None
    en: str | None = None

    @model_serializer(mode="wrap")
    def _omit_missing(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        dumped: dict[str, Any] = handler(self)
        return {k: v for k, v in dumped.items() if v is not None}


class View3dBinding(ApiModel):
    zone_id: int
    bubble_id: uuid.UUID
    title: LocalizedText | None = None
    description: LocalizedText | None = None


class View3d(ApiModel):
    scene: str
    bindings: list[View3dBinding]


# --- writing ------------------------------------------------------------------


def clean_text(value: str, *, multiline: bool) -> str:
    """Plain text made safe to store: Unicode NFC, control characters removed
    (line breaks survive when `multiline`, normalized to `\n`), trimmed.
    The content itself is never interpreted."""
    text = unicodedata.normalize("NFC", value)
    if multiline:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch
        for ch in text
        if unicodedata.category(ch) != "Cc" or (multiline and ch == "\n")
    ).strip()


def _cleaned(max_length: int, *, multiline: bool) -> AfterValidator:
    def check(value: str | None) -> str | None:
        if value is None:
            return None
        text = clean_text(value, multiline=multiline)
        if len(text) > max_length:
            raise ValueError(f"must be at most {max_length} characters")
        return text or None  # nothing left: the same as not given

    return AfterValidator(check)


TitleText = Annotated[str | None, _cleaned(TITLE_MAX_LENGTH, multiline=False)]
DescriptionText = Annotated[
    str | None, _cleaned(DESCRIPTION_MAX_LENGTH, multiline=True)
]


class _Strict(ApiModel):
    model_config = ConfigDict(extra="forbid")


class LocalizedTitleUpdate(_Strict):
    es: TitleText = None
    en: TitleText = None


class LocalizedDescriptionUpdate(_Strict):
    es: DescriptionText = None
    en: DescriptionText = None


class View3dBindingUpdate(_Strict):
    zone_id: int  # its range depends on the scene: checked with the scene
    bubble_id: uuid.UUID
    title: LocalizedTitleUpdate | None = None
    description: LocalizedDescriptionUpdate | None = None

    @field_validator("title", "description", mode="after")
    @classmethod
    def _drop_empty(
        cls, value: LocalizedTitleUpdate | LocalizedDescriptionUpdate | None
    ) -> LocalizedTitleUpdate | LocalizedDescriptionUpdate | None:
        """`{}` (or only blank languages) says nothing: store no override."""
        if value is not None and value.es is None and value.en is None:
            return None
        return value


class View3dUpdate(_Strict):
    scene: Annotated[str, Field(min_length=1, max_length=SCENE_MAX_LENGTH)]
    bindings: Annotated[list[View3dBindingUpdate], Field(max_length=MAX_BINDINGS)]

    def to_view3d(self) -> View3d:
        """The same content as the read model (what repositories take)."""
        return View3d.model_validate(self.model_dump())


def localized(raw: dict[str, Any] | None) -> LocalizedText | None:
    """A stored `{"es": ..., "en": ...}` as a model (None stays None)."""
    return LocalizedText.model_validate(raw) if raw else None
