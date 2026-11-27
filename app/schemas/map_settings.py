"""Per-map presentation settings (the frontend renders them; we store them).

Every field has a default, and settings are always read through this model, so
a row saved before a field existed (or an empty `{}` column) still reads as a
complete, valid object. Unknown keys are rejected on write.
"""

import uuid
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, ConfigDict, Field

from app.schemas.base import ApiModel
from app.schemas.congratulations import Congratulations
from app.schemas.journey_texts import JourneyText
from app.schemas.skin import HexColor
from app.schemas.welcome import ClosingFlow, Welcome


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


class TrainerSettings(_Strict):
    """The practice trainer of the map: off unless the editor turns it on. The
    language and level are the editor's choice, never the student's."""

    enabled: bool = False
    language: Literal["en"] = "en"
    level: Literal["A1", "A2", "B1", "B2"] = "A1"


class AssessmentPolicy(_Strict):
    """One of the two exams: which form, how many attempts, how long."""

    form_id: Annotated[str, Field(min_length=1, max_length=80)]
    max_attempts: Annotated[int, Field(strict=True, gt=0, le=20)] = 1
    time_limit_min: Annotated[int, Field(strict=True, gt=0, le=480)] = 60


class Exemption(_Strict):
    enabled: bool = True


class AssessmentSettings(_Strict):
    """The native check-in and final exam. A pass in either completes the
    course (through the Moodle passback) while `exemption.enabled`."""

    enabled: bool = False
    check_in: AssessmentPolicy | None = None
    final: AssessmentPolicy | None = None
    pass_percent: Annotated[int, Field(strict=True, ge=1, le=100)] = 80
    pass_operator: Literal["gt", "gte"] = "gt"
    exemption: Exemption = Field(default_factory=Exemption)


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
    # The closing flow, shown when the level is finished: the same shape as the
    # welcome flow (see `app.welcome_flow.flow` for what it may hold). Absent or
    # null: there is none.
    closing: ClosingFlow | None = None
    trainer: TrainerSettings = Field(default_factory=TrainerSettings)
    assessments: AssessmentSettings = Field(default_factory=AssessmentSettings)
    # What the congratulations screens say (see `app.schemas.congratulations`).
    # Absent or null: the frontend's default copy.
    congratulations: Congratulations | None = None
    # The other texts of the closing journey (see `app.schemas.journey_texts`).
    # Absent or null: the frontend's default copy.
    failure: JourneyText | None = Field(
        default=None,
        description="Shown after a failed attempt. Plain text; may contain "
        "{attempt}, {attempts_max}, {attempts_left} and {threshold}.",
    )
    exhausted: JourneyText | None = Field(
        default=None,
        description="Shown when every attempt at the final is used without "
        "passing. Plain text; no placeholders.",
    )
    route_instructions: JourneyText | None = Field(
        default=None,
        description="Shown on the map after a failed check-in. Plain text; no "
        "placeholders.",
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
        # Same rule for the trainer and the assessments: all defaults (off) are
        # left out so no stored map or publication changes its hash.
        if self.trainer == TrainerSettings():
            del stored["trainer"]
        if self.assessments == AssessmentSettings():
            del stored["assessments"]
        if self.congratulations is None or self.congratulations.is_empty():
            # Same rule for the texts: no custom copy is no key.
            del stored["congratulations"]
        for text, key in (
            (self.failure, "failure"),
            (self.exhausted, "exhausted"),
            (self.route_instructions, "routeInstructions"),
        ):
            if text is None or text.is_empty():
                del stored[key]
        if self.closing is None:
            # No closing flow is no key, so every map stored before it existed
            # keeps its hash.
            del stored["closing"]
        if self.welcome.is_default():
            del stored["welcome"]
        elif self.welcome.steps is None:
            # Same rule for the flow: a curtain without one has no `steps` key,
            # so every curtain stored before flows existed keeps its hash.
            del stored["welcome"]["steps"]
        return stored
