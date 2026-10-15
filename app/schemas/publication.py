import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints, field_validator

from app.enums import PublicationStatus
from app.schemas.base import ApiModel
from app.schemas.course_map import CourseMapRead

NOTE_MAX_LENGTH = 200


class PublicationRecord(ApiModel):
    """A `publications` row: what repositories return."""

    id: uuid.UUID
    course_map_id: uuid.UUID
    number: int
    snapshot: dict[str, Any]
    content_hash: str
    asset_ids: list[uuid.UUID]
    note: str | None
    created_at: datetime


class PublicationMeta(ApiModel):
    """A publication without its snapshot: enough to compare and to show."""

    course_map_id: uuid.UUID
    number: int
    content_hash: str
    created_at: datetime
    note: str | None


class PublicationChanges(ApiModel):
    """The draft against the current publication, for the editor's summary."""

    background_changed: bool
    title_changed: bool
    settings_changed: bool
    skins_changed: bool
    bubbles_added: int
    bubbles_removed: int
    bubbles_moved: int
    bubbles_restyled: int
    # Additive to the agreed contract: the guided path was reordered.
    bubbles_reordered: bool
    # The 3D scene or its zone bindings differ. Aliased by hand: the camelCase
    # generator would spell it `view3DChanged`.
    view3d_changed: bool = Field(alias="view3dChanged")


class PublicationState(ApiModel):
    status: PublicationStatus
    current_number: int | None
    current_published_at: datetime | None
    current_note: str | None
    draft_hash: str
    changes: PublicationChanges | None  # None until the first publication


class PublishRequest(ApiModel):
    """`draft_hash` is the hash the editor reviewed (from `publication-state`)."""

    draft_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    note: str | None = None

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if len(cleaned) > NOTE_MAX_LENGTH:
            raise ValueError(f"note must be at most {NOTE_MAX_LENGTH} characters")
        return cleaned or None


class PublicationSummary(ApiModel):
    number: int
    created_at: datetime
    note: str | None
    title: str
    bubble_count: int


class PublicationList(ApiModel):
    items: list[PublicationSummary]


class RestoreWarning(ApiModel):
    """Something the restore had to change to fit today's data."""

    code: str
    message: str


class RestoreResult(ApiModel):
    map: CourseMapRead
    warnings: list[RestoreWarning]


class PublishProblem(ApiModel):
    code: str
    message: str
