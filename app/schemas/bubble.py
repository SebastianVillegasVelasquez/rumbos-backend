import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import Field, model_validator

from app.enums import BubbleIcon, BubbleKind, BubbleStatus
from app.schemas.base import ApiModel
from app.schemas.content import BlocksContent, WidgetContent

# Relative position on the map, never pixels.
UnitFloat = Annotated[float, Field(ge=0, le=1)]


class BubbleCreate(ApiModel):
    """A bubble for a Moodle activity (`activityId`), or a content bubble
    (`kind: "content"` with `content`). `content` is kept raw here and checked
    by `app.schemas.content.parse_content`, so its problems come back as one
    `content_invalid` answer instead of a generic validation error."""

    kind: BubbleKind = BubbleKind.ACTIVITY
    activity_id: int | None = None
    content: dict[str, Any] | None = None
    x: UnitFloat
    y: UnitFloat
    icon: BubbleIcon | None = None
    status: BubbleStatus = BubbleStatus.LOCKED

    @model_validator(mode="after")
    def _check_kind(self) -> Self:
        if self.kind is BubbleKind.ACTIVITY:
            if self.activity_id is None:
                raise ValueError("activityId is required for an activity bubble")
            if self.content is not None:
                raise ValueError("content is only for kind 'content'")
        else:
            if self.activity_id is not None:
                raise ValueError("a content bubble has no activityId")
            if self.content is None:
                raise ValueError("content is required for kind 'content'")
        return self


class BubbleUpdate(ApiModel):
    """Partial update: only fields present in the request are applied.

    `icon` and `skinId` are nullable, so an explicit `null` (clear it) is
    different from omitting it; use `model_fields_set` to tell them apart.
    `x` and `y` always move together. `sequence` is not here on purpose: the
    order endpoint is the only thing that changes it.
    """

    x: UnitFloat | None = None
    y: UnitFloat | None = None
    icon: BubbleIcon | None = None
    status: BubbleStatus | None = None
    # Nullable like `icon`: an explicit `"skinId": null` clears the bubble's
    # own skin (it falls back to its map's rules and default).
    skin_id: uuid.UUID | None = None
    # Replaces the whole content of a content bubble (checked like on create).
    content: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        sent = self.model_fields_set
        if not sent:
            raise ValueError("at least one field must be provided")
        for name in ("x", "y", "status", "content"):
            if name in sent and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        if ("x" in sent) != ("y" in sent):
            raise ValueError("x and y must be provided together")
        return self


class BubbleRead(ApiModel):
    id: uuid.UUID
    course_map_id: uuid.UUID
    # Null for a content bubble.
    activity_id: int | None
    x: float
    y: float
    icon: BubbleIcon | None
    status: BubbleStatus
    skin_id: uuid.UUID | None
    sequence: int
    kind: BubbleKind = BubbleKind.ACTIVITY
    # What a content bubble shows; null for an activity bubble.
    content: BlocksContent | WidgetContent | None = Field(
        default=None, discriminator="type"
    )
    # Concurrency token: starts at 1, bumped by edits to x, y, icon or skin.
    version: int = 1
    created_at: datetime
    updated_at: datetime


class BubbleOrderUpdate(ApiModel):
    """The full desired order of a map's bubbles (the guided path)."""

    bubble_ids: list[uuid.UUID]


class BubbleOrdered(ApiModel):
    bubbles: list[BubbleRead]
