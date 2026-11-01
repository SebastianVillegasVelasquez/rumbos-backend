"""Request and response bodies of the export and import endpoints."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.portability.format import (
    INT32_MAX,
    MAX_BUBBLES_PER_MAP,
    MAX_MAPS,
    NAME_MAX,
)
from app.schemas.base import ApiModel
from app.schemas.content import Line
from app.schemas.course_map import CourseMapSummary, Modname

MAX_EXPORT_ACTIVITIES = 2_000
# Every descriptor of an import has a key in the mapping.
MAX_MAPPING_ENTRIES = MAX_MAPS * MAX_BUBBLES_PER_MAP

WARNING_CODES = (
    "widgets_disabled",
    "scene_unknown",
    "audio_skipped",
    "library_track_unavailable",
    "audio_uploads_disabled",
    # Not in the first contract: a built-in skin this deployment does not have.
    "skin_unavailable",
)


class _Strict(ApiModel):
    model_config = ConfigDict(extra="forbid")


# --- export ----------------------------------------------------------------------


class ExportActivity(_Strict):
    """What the client knows about an activity of the course. Display data to put
    in the archive: untrusted, never used to decide anything."""

    cmid: int = Field(ge=1, le=INT32_MAX)
    name: Line = Field(min_length=1, max_length=NAME_MAX)
    modname: Modname
    section_number: int = Field(ge=0, le=10_000)
    section_name: Line = Field(max_length=NAME_MAX)
    position_in_section: int = Field(ge=0, le=10_000)


class ExportRequest(_Strict):
    map_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_MAPS)
    source: Literal["draft", "saved"] = "draft"
    include_own_audio: bool = False
    activities: list[ExportActivity] = Field(
        default_factory=list, max_length=MAX_EXPORT_ACTIVITIES
    )

    @model_validator(mode="after")
    def _distinct_maps(self) -> Self:
        if len(set(self.map_ids)) != len(self.map_ids):
            raise ValueError("a map is listed twice")
        return self


# --- import ----------------------------------------------------------------------


class ImportWarning(ApiModel):
    code: str
    message: str


class PreviewMap(ApiModel):
    ref: str
    title: str
    bubble_count: int
    activity_bubbles: int
    content_bubbles: int
    has_welcome: bool
    has_audio: bool
    has_view3d: bool


class Descriptor(ApiModel):
    """An activity bubble of the archive, for the frontend to find in the course."""

    key: str
    map_ref: str
    bubble_ref: str
    modname: str
    name: str
    section_number: int
    section_name: str
    position_in_section: int


class PreviewAssets(ApiModel):
    count: int
    size_bytes: int = Field(alias="bytes")


class OwnTrack(ApiModel):
    title: str
    credit: str | None
    license: str | None


class LibraryTrack(ApiModel):
    title: str
    available: bool


class PreviewAudio(ApiModel):
    own_tracks: list[OwnTrack]
    library_tracks: list[LibraryTrack]


class ImportPreview(ApiModel):
    format_version: int
    maps: list[PreviewMap]
    descriptors: list[Descriptor]
    assets: PreviewAssets
    audio: PreviewAudio
    custom_skins: int
    warnings: list[ImportWarning]


class ImportStaged(ApiModel):
    import_id: uuid.UUID
    expires_at: datetime
    preview: ImportPreview


class ApplyRequest(_Strict):
    # Checked by hand: a bad value is `mapping_invalid`, not a generic 422.
    mapping: dict[str, Any] = Field(max_length=MAX_MAPPING_ENTRIES)
    audio_rights_declared: Annotated[bool, Field(strict=True)] = False


class ApplyResult(ApiModel):
    maps: list[CourseMapSummary]
    skipped_bubbles: int
    warnings: list[ImportWarning]
