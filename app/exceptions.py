"""Domain errors, raised by repositories/services and mapped to HTTP in the API layer."""

import uuid


class DomainError(Exception):
    """Base class for expected, business-level failures."""


class CourseMapNotFoundError(DomainError):
    pass


class BubbleNotFoundError(DomainError):
    pass


class ActivityAlreadyPlacedError(DomainError):
    """The Moodle activity already has a bubble on one of the course's maps.

    `course_map_id` / `course_map_title` say where, when the repository could
    find out (it may not if the other bubble vanished in the meantime).
    """

    def __init__(
        self,
        activity_id: int,
        course_map_id: uuid.UUID | None = None,
        course_map_title: str | None = None,
    ) -> None:
        super().__init__(activity_id)
        self.activity_id = activity_id
        self.course_map_id = course_map_id
        self.course_map_title = course_map_title


class SectionAlreadyMappedError(DomainError):
    """The course already has a map for this Moodle section."""


class OrderMismatchError(DomainError):
    """An ordering request does not list exactly the existing ids."""

    def __init__(
        self,
        missing: list[uuid.UUID],
        unexpected: list[uuid.UUID],
        duplicated: list[uuid.UUID],
    ) -> None:
        super().__init__("order does not match the existing items")
        self.missing = missing
        self.unexpected = unexpected
        self.duplicated = duplicated
