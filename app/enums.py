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


class BubbleIcon(StrEnum):
    QUESTION = "question"
    CHEST = "chest"
    STAR = "star"
    FLAG = "flag"
    BOOK = "book"
    VIDEO = "video"
    TROPHY = "trophy"
    LOCK = "lock"
