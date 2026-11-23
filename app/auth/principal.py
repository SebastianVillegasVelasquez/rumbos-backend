import uuid
from dataclasses import dataclass, field

from app.enums import Role


@dataclass(frozen=True)
class Principal:
    """Who a request acts as: the tenant, the one course it may touch, the role.

    `course_id` is None only for the legacy anonymous principal, which is not
    tied to a course (the standalone deployment serves many).
    """

    tenant_id: uuid.UUID
    course_id: int | None
    role: Role
    sub: str | None
    # The Moodle user id the plugin signed, if it did. Kept out of repr so it
    # cannot reach a log line by accident.
    moodle_user_id: int | None = field(default=None, repr=False)

    def can_see_course(self, course_id: int) -> bool:
        """Whether the course is one this principal may act on."""
        return self.course_id is None or self.course_id == course_id
