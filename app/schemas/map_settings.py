"""Per-map presentation settings (the frontend renders them; we store them).

Every field has a default, and settings are always read through this model, so
a row saved before a field existed (or an empty `{}` column) still reads as a
complete, valid object. Unknown keys are rejected on write.
"""

import uuid
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, ConfigDict, Field

from app.schemas.base import ApiModel
from app.schemas.skin import HexColor
from app.schemas.welcome import Welcome


class _Strict(ApiModel):
    model_config = ConfigDict(extra="forbid")


class InitialView(_Strict):
    """Where the camera starts: a point of the map (0-1) and a zoom."""

    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]
    zoom: Annotated[float, Field(ge=0.25, le=4)]


class PathSettings(_Strict):
    visible: bool = True
    style: Literal["dashed", "solid", "dotted"] = "dashed"
    color: HexColor | None = None  # None: the frontend picks one
    animated: bool = True


class AmbientSettings(_Strict):
    kind: Literal["none", "fireflies", "snow", "leaves", "clouds", "sparkles"] = "none"
    intensity: Annotated[float, Field(ge=0, le=1)] = 0.5


class MapAudio(_Strict):
    """The map's background music: an `audio` asset (the tenant's own or the
    library's) and how the player should play it."""

    asset_id: uuid.UUID
    volume: Annotated[float, Field(ge=0, le=1)] = 0.4
    loop: bool = True
    # Browsers refuse to start sound before the user has touched the page.
    start_after_interaction: bool = False


class AudioInfo(ApiModel):
    """What a player shows about a map's music. Read-only: a draft reads it
    live from the asset, a publication freezes `title`, `credit` and
    `duration_seconds`.

    The rest is read live on every request, for drafts and publications alike,
    and is never part of a snapshot or of its hash: `available` (false when the
    audio file is missing from the storage), `license` and `cover_url` (the
    track's licence text, and where its cover is, if it has one)."""

    title: str
    credit: str
    duration_seconds: float
    available: bool = True
    license: str | None = None
    cover_url: str | None = None


def _null_is_default(value: Any) -> Any:
    return Welcome() if value is None else value


class MapSettings(_Strict):
    schema_version: Literal[1] = 1
    mode: Literal["explorative", "guided"] = "explorative"
    fit: Literal["original", "fit-width", "fit-height", "contain"] = "fit-width"
    initial_view: InitialView | None = None
    path: PathSettings = Field(default_factory=PathSettings)
    ambient: AmbientSettings = Field(default_factory=AmbientSettings)
    intro: Literal["none", "flyin"] = "none"
    audio: MapAudio | None = None
    # Reads always carry a complete curtain; `null` (or nothing) is all defaults.
    welcome: Annotated[Welcome, BeforeValidator(_null_is_default)] = Field(
        default_factory=Welcome
    )

    def to_stored(self) -> dict[str, Any]:
        """The JSON-able form kept in `course_maps.settings` (camelCase).

        `audio` is left out when there is no music, never written as null: maps
        published before music existed have no such key, and a draft that still
        has no music must build the very same text, and hash, as they do.

        `welcome` follows the same rule: a curtain that is all defaults is left
        out, so maps (and publications) from before it existed keep their hash
        and an editor who saves back what it read does not create a change."""
        stored = self.model_dump(by_alias=True, mode="json")
        if self.audio is None:
            del stored["audio"]
        if self.welcome.is_default():
            del stored["welcome"]
        elif self.welcome.steps is None:
            # Same rule for the flow: a curtain without one has no `steps` key,
            # so every curtain stored before flows existed keeps its hash.
            del stored["welcome"]["steps"]
        return stored
