"""Domain enums shared by ORM models and Pydantic schemas.

Kept free of SQLAlchemy/Pydantic imports so any layer can depend on it.
Values mirror the frontend's `BubbleStatus` and `IconKey` types.
"""

from enum import StrEnum


class BubbleStatus(StrEnum):
    LOCKED = "locked"
    NO_COMPLETE = "no_complete"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"


class MoodleStatus(StrEnum):
    """How fresh the Moodle data behind a resolved map is."""

    LIVE = "live"  # a live call, or a cache hit inside the TTL
    CACHED = "cached"  # stale copy served because Moodle failed
    UNAVAILABLE = "unavailable"  # nothing to serve


class Availability(StrEnum):
    """What a bubble's Moodle activity looks like to a learner."""

    AVAILABLE = "available"
    HIDDEN = "hidden"  # exists but learners cannot see it
    MISSING = "missing"  # gone from the course, or not a valid bubble target
    UNKNOWN = "unknown"  # Moodle could not be asked


class BubbleIcon(StrEnum):
    QUESTION = "question"
    CHEST = "chest"
    STAR = "star"
    FLAG = "flag"
    BOOK = "book"
    VIDEO = "video"
    TROPHY = "trophy"
    LOCK = "lock"


class AssetKind(StrEnum):
    """What an uploaded file is for; decides its limits."""

    BACKGROUND = "background"
    BUBBLE = "bubble"
    AUDIO = "audio"
    # A picture an editor puts inside a content bubble (not a skin, not a backdrop).
    IMAGE = "image"

    @property
    def has_thumbnail(self) -> bool:
        """Whether uploads of this kind get a small WebP preview next to them."""
        return self in (AssetKind.BACKGROUND, AssetKind.IMAGE)


class AssetScope(StrEnum):
    """Whose assets a listing shows."""

    TENANT = "tenant"  # the caller's own uploads
    LIBRARY = "library"  # the operator's, shared by every tenant


class PublicationStatus(StrEnum):
    """How a map's draft relates to what students see."""

    NEVER_PUBLISHED = "never_published"
    UP_TO_DATE = "up_to_date"
    UNPUBLISHED_CHANGES = "unpublished_changes"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class KeyStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class Role(StrEnum):
    """What a bearer token may do (see `app.auth.principal`)."""

    EDITOR = "editor"
    STUDENT = "student"
