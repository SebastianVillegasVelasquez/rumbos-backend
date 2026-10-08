"""Per-map presentation settings (the frontend renders them; we store them).

Every field has a default, and settings are always read through this model, so
a row saved before a field existed (or an empty `{}` column) still reads as a
complete, valid object. Unknown keys are rejected on write.
"""

from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field

from app.schemas.base import ApiModel
from app.schemas.skin import HexColor


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


class MapSettings(_Strict):
    schema_version: Literal[1] = 1
    mode: Literal["explorative", "guided"] = "explorative"
    fit: Literal["original", "fit-width", "fit-height", "contain"] = "fit-width"
    initial_view: InitialView | None = None
    path: PathSettings = Field(default_factory=PathSettings)
    ambient: AmbientSettings = Field(default_factory=AmbientSettings)
    intro: Literal["none", "flyin"] = "none"

    def to_stored(self) -> dict[str, Any]:
        """The JSON-able form kept in `course_maps.settings` (camelCase)."""
        return self.model_dump(by_alias=True, mode="json")
