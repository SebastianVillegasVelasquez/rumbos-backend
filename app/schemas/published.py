"""What students see: the latest publication of a map, joined with live data.

Everything comes from the snapshot EXCEPT `position`, `moodleSectionId` and
each bubble's `status`, which are read from the live rows.
"""

import uuid
from datetime import datetime
from typing import Any

from app.enums import BubbleIcon, BubbleStatus
from app.schemas.base import ApiModel
from app.schemas.course_map import SkinRule
from app.schemas.map_settings import MapSettings


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
    publication_number: int
    published_at: datetime
