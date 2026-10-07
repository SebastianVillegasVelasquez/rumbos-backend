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
from typing import Protocol

from app.schemas.bubble import BubbleCreate, BubbleRead, BubbleUpdate
from app.schemas.course_map import (
    CourseMapBase,
    CourseMapCreate,
    CourseMapSummary,
    CourseMapUpdate,
)


class CourseMapRepository(Protocol):
    async def get_by_id(self, course_map_id: uuid.UUID) -> CourseMapBase | None: ...

    async def get_by_moodle_course_id(
        self, moodle_course_id: int
    ) -> CourseMapBase | None: ...

    async def list(
        self,
        moodle_course_id: int | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[CourseMapSummary], int]:
        """Maps ordered by `updated_at` desc, and the total before paging.

        `q` is a case-insensitive substring match on the title.
        """
        ...

    async def create(self, data: CourseMapCreate) -> CourseMapBase:
        """Raises `CourseMapAlreadyExistsError` if the Moodle course has a map."""
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


class BubbleRepository(Protocol):
    async def get_by_id(self, bubble_id: uuid.UUID) -> BubbleRead | None: ...

    async def list_by_course_map(
        self, course_map_id: uuid.UUID
    ) -> list[BubbleRead]: ...

    async def create(self, course_map_id: uuid.UUID, data: BubbleCreate) -> BubbleRead:
        """Raises `CourseMapNotFoundError` if the map doesn't exist."""
        ...

    async def update(
        self, bubble_id: uuid.UUID, data: BubbleUpdate
    ) -> BubbleRead | None:
        """Applies only the fields set in `data`, all in one statement.

        Either every provided field is persisted or none is.
        """
        ...

    async def delete(self, bubble_id: uuid.UUID) -> bool: ...
