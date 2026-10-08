"""Skin configuration: the shared contract with the frontend, validated here.

A skin is stored as JSON (camelCase keys, exactly as the API shows it). The
models forbid unknown keys, so what is stored is always what the contract
describes. `schemaVersion` is 1 and defaults to it.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from app.enums import BubbleIcon
from app.schemas.base import ApiModel

SKIN_NAME_MAX_LENGTH = 60
MAX_SKINS_LISTED = 200

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]
SkinName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=SKIN_NAME_MAX_LENGTH
    ),
]
LabelMode = Literal["hover", "always", "never"]
Completion = Literal["none", "ripple", "burst"]


class _Strict(ApiModel):
    """Config objects reject keys the contract does not define."""

    model_config = ConfigDict(extra="forbid")


class StateColors(_Strict):
    fill: HexColor
    accent: HexColor
    glow: HexColor


class SkinPalette(_Strict):
    locked: StateColors
    available: StateColors
    in_progress: StateColors
    complete: StateColors


class SkinIcon(_Strict):
    mode: Literal["auto", "preset", "none"]
    preset: BubbleIcon | None = None
    color: Literal["auto"] | HexColor

    @model_validator(mode="after")
    def _preset_mode_needs_a_preset(self) -> Self:
        if self.mode == "preset" and self.preset is None:
            raise ValueError("preset is required when mode is 'preset'")
        return self


class SkinLabel(_Strict):
    mode: LabelMode


class ProceduralEffects(_Strict):
    idle: Literal["none", "float", "pulse", "breathe"]
    ring: bool
    glow: bool
    completion: Completion


class ImageEffects(_Strict):
    idle: Literal["none", "float", "breathe"]
    completion: Completion


class ProceduralSkin(_Strict):
    schema_version: Literal[1] = 1
    kind: Literal["procedural"]
    shape: Literal["circle", "hexagon", "badge", "pin"]
    size: Annotated[int, Field(ge=32, le=160)]  # design units, not pixels
    palette: SkinPalette
    icon: SkinIcon
    label: SkinLabel
    effects: ProceduralEffects


class ImageStates(_Strict):
    """Bubble-asset ids per state. Only `available` is required."""

    available: uuid.UUID
    locked: uuid.UUID | None = None
    next: uuid.UUID | None = None
    in_progress: uuid.UUID | None = None
    complete: uuid.UUID | None = None
    hover: uuid.UUID | None = None

    def referenced(self) -> dict[str, uuid.UUID]:
        """State name (camelCase) -> asset id, for the states that are set."""
        return {
            alias: value
            for name, alias in (
                ("available", "available"),
                ("locked", "locked"),
                ("next", "next"),
                ("in_progress", "inProgress"),
                ("complete", "complete"),
                ("hover", "hover"),
            )
            if (value := getattr(self, name)) is not None
        }


class ImageSkin(_Strict):
    schema_version: Literal[1] = 1
    kind: Literal["image"]
    size: Annotated[int, Field(ge=32, le=320)]  # design units, not pixels
    anchor: Literal["center", "bottom"]
    states: ImageStates
    label: SkinLabel
    effects: ImageEffects


SkinConfig = Annotated[ProceduralSkin | ImageSkin, Field(discriminator="kind")]

# For code that parses a stored JSON config outside a request model.
skin_config_adapter: TypeAdapter[ProceduralSkin | ImageSkin] = TypeAdapter(SkinConfig)


def dump_config(config: ProceduralSkin | ImageSkin) -> dict[str, Any]:
    """The JSON-able form that is stored: camelCase keys, UUIDs as strings."""
    return config.model_dump(by_alias=True, mode="json")


class SkinCreate(ApiModel):
    name: SkinName
    config: SkinConfig


class SkinUpdate(ApiModel):
    """Partial update: only fields present in the request are applied."""

    name: SkinName | None = None
    config: SkinConfig | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        sent = self.model_fields_set
        if not sent:
            raise ValueError("at least one field must be provided")
        for field in sent:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class SkinRead(ApiModel):
    id: uuid.UUID
    name: str
    builtin: bool
    is_default: bool
    config: SkinConfig
    created_at: datetime
    updated_at: datetime


class SkinList(ApiModel):
    items: list[SkinRead]
