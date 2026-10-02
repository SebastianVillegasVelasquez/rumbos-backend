"""Domain errors, raised by repositories/services and mapped to HTTP in the API layer."""


class DomainError(Exception):
    """Base class for expected, business-level failures."""


class CourseMapNotFoundError(DomainError):
    pass


class BubbleNotFoundError(DomainError):
    pass


class CourseMapAlreadyExistsError(DomainError):
    """A map already exists for this Moodle course (one map per course)."""
