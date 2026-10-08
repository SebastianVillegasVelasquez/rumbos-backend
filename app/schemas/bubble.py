import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import Field, model_validator

from app.enums import BubbleIcon, BubbleStatus
from app.schemas.base import ApiModel

# Relative position on the map, never pixels.
UnitFloat = Annotated[float, Field(ge=0, le=1)]


class BubbleCreate(ApiModel):
    activity_id: int
    x: UnitFloat
    y: UnitFloat
    icon: BubbleIcon | None = None
    status: BubbleStatus = BubbleStatus.LOCKED


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

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        sent = self.model_fields_set
        if not sent:
            raise ValueError("at least one field must be provided")
        for name in ("x", "y", "status"):
            if name in sent and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        if ("x" in sent) != ("y" in sent):
            raise ValueError("x and y must be provided together")
        return self


class BubbleRead(ApiModel):
    id: uuid.UUID
    course_map_id: uuid.UUID
    activity_id: int
    x: float
    y: float
    icon: BubbleIcon | None
    status: BubbleStatus
    skin_id: uuid.UUID | None
    sequence: int
    created_at: datetime
    updated_at: datetime
