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
    """What an uploaded image is for; decides its size limits."""

    BACKGROUND = "background"
    BUBBLE = "bubble"
