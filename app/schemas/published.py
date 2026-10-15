"""What students see: the latest publication of a map, joined with live data.

Everything comes from the snapshot EXCEPT `position`, `moodleSectionId` and
each bubble's `status`, which are read from the live rows.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.enums import BubbleIcon, BubbleStatus
from app.schemas.base import ApiModel
from app.schemas.course_map import SkinRule
from app.schemas.map_settings import MapSettings
from app.schemas.view3d import View3d


class PublishedRow(ApiModel):
    """A map's latest publication with its live columns: what repositories return."""

    course_map_id: uuid.UUID
    moodle_course_id: int
    moodle_section_id: int | None
    position: int
    number: int
    published_at: datetime
    snapshot: dict[str, Any]


class PublishedMapSummary(ApiModel):
    id: uuid.UUID
    title: str
    moodle_course_id: int
    moodle_section_id: int | None
    position: int
    image_url: str
    bubble_count: int
    complete_count: int
    publication_number: int
    published_at: datetime


class PublishedMapList(ApiModel):
    items: list[PublishedMapSummary]
    total: int
    limit: int
    offset: int


class PublishedSkin(ApiModel):
    name: str
    is_default: bool
    config: dict[str, Any]  # the frozen config, exactly as published


class PublishedBubble(ApiModel):
    id: uuid.UUID
    activity_id: int
    x: float
    y: float
    icon: BubbleIcon | None
    skin_id: uuid.UUID | None
    sequence: int
    status: BubbleStatus


class PublishedMapRead(ApiModel):
    id: uuid.UUID
    title: str
    moodle_course_id: int
    moodle_section_id: int | None
    position: int
    image_url: str
    settings: MapSettings
    default_skin_id: uuid.UUID | None
    skin_rules: list[SkinRule]
    skins: dict[str, PublishedSkin]
    bubbles: list[PublishedBubble]
    # The frozen 3D scene and zone bindings; null for a map published without
    # one. Aliased by hand: the camelCase generator would spell it `view3D`.
    view3d: View3d | None = Field(alias="view3d")
    publication_number: int
    published_at: datetime
