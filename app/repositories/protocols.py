"""Repository interfaces the service layer depends on.

These are structural `Protocol`s: any class with matching async methods
satisfies them, with no inheritance. Inputs and outputs are Pydantic
schemas so callers never import SQLAlchemy models.

Contract shared by all implementations:
- Reads (`get_*`, `list_*`) never commit.
- Writes (`create`, `update`, `delete`) are atomic: they commit on
  success and roll back before re-raising on failure.
- A missing target is reported as `None` / `False`, not an exception.
"""

import uuid
from collections.abc import Collection
from typing import Protocol

from app.enums import AssetKind
from app.schemas.asset import AssetRecord
from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import (
    CourseMapBase,
    CourseMapCreate,
    CourseMapSummary,
    CourseMapUpdate,
)


class CourseMapRepository(Protocol):
    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapBase | None: ...

    async def list_for_course(self, moodle_course_id: int) -> list[CourseMapSummary]:
        """Every map of the course, unpaged, in level order."""
        ...

    async def create(self, data: CourseMapCreate) -> CourseMapBase:
        """Appends the map to its course (position = last + 1).

        Raises `SectionAlreadyMappedError` if the course already has a map for
        `data.moodle_section_id`.
        """
        ...

    async def set_order(
        self, moodle_course_id: int, ordered_ids: list[uuid.UUID]
    ) -> None:
        """Sets `position` 0..n-1 following `ordered_ids`, atomically."""
        ...

    async def update(
        self, course_map_id: uuid.UUID, data: CourseMapUpdate
    ) -> CourseMapBase | None:
        """Applies only the fields set in `data`, all in one statement.

        Either every provided field is persisted or none is. Returns None if
        the map doesn't exist.
        """
        ...

    async def delete(self, course_map_id: uuid.UUID) -> bool:
        """Deletes the map and its bubbles. Returns False if it didn't exist."""
        ...

    async def list(
        self,
        moodle_course_id: int | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[CourseMapSummary], int]:
        """Maps and the total before paging.

        Ordered by `(position, created_at, id)` when `moodle_course_id` is
        given (the course's levels), otherwise by `updated_at` desc.
        `q` is a case-insensitive substring match on the title.
        """
        ...


class BubbleRepository(Protocol):
    async def get_by_id(self, bubble_id: uuid.UUID) -> BubbleRead | None: ...

    async def list_by_course_map(
        self, course_map_id: uuid.UUID
    ) -> list[BubbleRead]: ...

    async def list_by_course(self, moodle_course_id: int) -> list[BubbleRead]:
        """Every bubble on any map of the Moodle course."""
        ...

    async def create(
        self, course_map_id: uuid.UUID, moodle_course_id: int, data: BubbleCreate
    ) -> BubbleRead:
        """`moodle_course_id` is the parent map's, copied onto the bubble.

        Raises `CourseMapNotFoundError` if the map doesn't exist (or is not of
        that course), and `ActivityAlreadyPlacedError` if the activity already
        has a bubble on any map of the course.
        """
        ...

    async def update(
        self, bubble_id: uuid.UUID, data: BubbleUpdate
    ) -> BubbleRead | None:
        """Applies only the fields set in `data`, all in one statement.

        Either every provided field is persisted or none is.
        """
        ...

    async def delete(self, bubble_id: uuid.UUID) -> bool: ...


class AssetRepository(Protocol):
    async def get_by_id(self, asset_id: uuid.UUID) -> AssetRecord | None: ...

    async def get_many(
        self, asset_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, AssetRecord]:
        """The assets that exist among `asset_ids`, by id."""
        ...

    async def find_by_hash(
        self, kind: AssetKind, sha256: str
    ) -> AssetRecord | None: ...

    async def total_bytes(self) -> int:
        """Sum of the stored size of every asset (what the quota counts)."""
        ...

    async def create(
        self,
        asset_id: uuid.UUID,
        kind: AssetKind,
        mime: str,
        width: int,
        height: int,
        size_bytes: int,
        sha256: str,
    ) -> AssetRecord:
        """Raises `AssetAlreadyExistsError` if the kind and hash already exist."""
        ...
