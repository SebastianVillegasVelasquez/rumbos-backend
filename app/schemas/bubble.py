import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.enums import BubbleIcon, BubbleStatus

# Relative position on the map, never pixels.
UnitFloat = Annotated[float, Field(ge=0, le=1)]


class BubbleCreate(BaseModel):
    activity_id: int
    x: UnitFloat
    y: UnitFloat
    icon: BubbleIcon | None = None
    status: BubbleStatus = BubbleStatus.LOCKED


class BubbleUpdate(BaseModel):
    """Partial update: only fields present in the request are applied.

    `icon` is nullable, so an explicit `"icon": null` (clear the icon) is
    different from omitting it; use `model_fields_set` to tell them apart.
    `x` and `y` always move together.
    """

    x: UnitFloat | None = None
    y: UnitFloat | None = None
    icon: BubbleIcon | None = None
    status: BubbleStatus | None = None

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


class BubbleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_map_id: uuid.UUID
    activity_id: int
    x: float
    y: float
    icon: BubbleIcon | None
    status: BubbleStatus
    created_at: datetime
    updated_at: datetime
